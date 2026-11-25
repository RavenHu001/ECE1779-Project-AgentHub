# Serve authenticated chat requests; background processing runs in a separate container.
# 提供经过身份验证的聊天接口；后台处理在独立容器运行。

import logging
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import router as auth_router
from app.db import SessionLocal, engine, get_session
from app.event_store import (
    EventToAppend,
    append_events,
    job_status_event,
    message_created_event,
    read_event_head,
)
from app.events import (
    ConversationStream,
    DurableConversationStream,
    conversation_events,
    durable_cursor,
    parse_durable_cursor,
    parse_last_event_id,
)
from app.history import read_execution_detail, read_history, read_history_snapshot
from app.models import (
    AgentStep,
    Conversation,
    ConversationEventHead,
    Execution,
    Job,
    Message,
    ProposalDecision,
    ResolutionProposal,
    User,
)
from app.resolution_workflow import (
    EXPIRED_REPLY,
    PROPOSAL_VERSION,
    active_resolution_exists,
    close_proposal,
    invalidate_proposal,
    read_invalidation_reason,
    unfinished_turn_before,
)
from app.schemas import (
    AgentStepOut,
    ConflictCode,
    ConflictOut,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    ExecutionDetailOut,
    JobOut,
    MessageCreate,
    MessageOut,
    ProposalDecisionIn,
    ProposalDecisionOut,
    SubmissionOut,
)
from app.security import current_user, session_user

app = FastAPI(
    title="AgentHub Backend", version="0.2.0", description="Durable chat jobs with optional model replies."
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(auth_router)
log = logging.getLogger(__name__)


class ConflictError(Exception):
    def __init__(self, code: ConflictCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@app.exception_handler(ConflictError)
def conflict_error(_request: Request, exc: ConflictError):
    return JSONResponse(status_code=409, content=ConflictOut(detail=exc.detail, code=exc.code).model_dump())


@app.get("/", include_in_schema=False)
def demo():
    """Serve the public development client; business requests still require authentication.
    提供公开的开发用聊天页面；页面发出的业务请求仍需通过身份认证。
    """
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# Return a safe English error without exposing database parameters.
# 返回安全的英文错误，不公开数据库参数。
@app.exception_handler(SQLAlchemyError)
def database_error(_request: Request, exc: SQLAlchemyError):
    """Expose a retryable database outage without serializing SQL or connection details.
    返回可稍后重试的数据库不可用响应，不公开 SQL 参数或数据库连接详情。
    """
    log.error("Database operation failed: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


# Check database connectivity without requiring a user account.
# 无需用户账号即可检查数据库连通性。
@app.get("/health", description="Check API and database availability. Authentication is not required.")
def health():
    """Probe database connectivity and report configured mode without calling a model.
    探测数据库连接并报告配置的回复模式，此接口不调用模型或验证 Worker 心跳。
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    # This is configured mode, not a claim that the worker/model is healthy.
    # 此字段仅代表配置值，不表示 Worker 或模型服务已通过可用性验证。
    return {"status": "ok", "database": "ok", "reply_mode": os.getenv("REPLY_MODE", "fixed")}


# Return the authenticated account without its password hash.
# 返回已验证账号的信息，不返回密码哈希。
@app.get("/me", description="Return the authenticated account without password or credential fields.")
def me(response: Response, user: User = Depends(current_user)):
    """Return only the authenticated account's public identity fields.
    仅返回已验证账号的公开身份字段，不包含密码哈希或其他认证信息。
    """
    response.headers["Cache-Control"] = "no-store"
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# Derive ownership from authentication rather than the request body.
# 从身份验证结果确定归属，不从请求体读取归属用户。
@app.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=201,
    description="Create a conversation owned by the authenticated account. The default title is New conversation.",
)
def create_conversation(
    payload: ConversationCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    """Create an owned conversation and its zero event cursor in one transaction.
    在同一事务中为已验证用户创建会话及初始值为零的事件游标。
    """
    with session.begin():
        conversation = Conversation(user_id=user.id, title=payload.title)
        session.add(conversation)
        session.flush()
        # The generated conversation ID must exist before its event counter can reference it.
        # 必须先写入会话编号，事件计数器才能引用它。
        session.add(ConversationEventHead(conversation_id=conversation.id))
    return conversation


# Restrict the list to the current account and bound its size.
# 只列出当前账号的会话，并限制返回数量。
@app.get(
    "/conversations",
    response_model=list[ConversationOut],
    description="List your conversations, pinned first, then most recently updated.",
)
def list_conversations(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Return a bounded account-scoped list, newest activity first with deterministic ties.
    返回数量受限且仅属于当前账号的会话，按最近活动排序并用编号稳定处理并列情况。
    """
    return session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(Conversation.is_pinned.desc(), Conversation.updated_at.desc(), Conversation.id)
        .limit(limit)
    ).all()


@app.patch("/conversations/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Apply explicit title/pin values under the same lock as message submissions.
    与消息提交使用相同的锁，写入明确指定的标题或置顶值。
    """
    with session.begin():
        conversation = session.scalar(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(conversation, key, value)
        conversation.updated_at = func.clock_timestamp()
    return conversation


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: UUID,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Hide settled chats without deleting refunds or leaving hidden work running.
    隐藏已结束的聊天，保留退款记录且不留下隐藏的运行任务。
    """
    with session.begin():
        conversation = session.scalar(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        if conversation.deleted_at is not None:
            return Response(status_code=204)
        active_job = session.scalar(
            select(Job.id)
            .join(Message, Job.source_message_id == Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Job.status.in_(("QUEUED", "RUNNING")),
            )
            .limit(1)
        )
        if active_job or active_resolution_exists(session, conversation_id):
            raise HTTPException(
                409, "Finish active work or reject the pending proposal before deleting this chat."
            )
        conversation.deleted_at = func.clock_timestamp()
        conversation.is_pinned = False
    return Response(status_code=204)


# Lock one conversation and atomically save its message, sequence and job.
# 锁定对应会话，在同一事务内保存消息、顺序号和待办。
@app.post(
    "/conversations/{conversation_id}/messages",
    response_model=SubmissionOut,
    status_code=202,
    responses={409: {"model": ConflictOut, "description": "Message submission conflict"}},
    description="Save a message and its background job atomically. Reuse submission_key only when retrying the same request.",
)
def submit_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Atomically save the message, job and their events, with stable IDs for retries.
    原子保存消息、待办及其事件，重复提交返回原有编号。
    """
    with session.begin():
        # Ownership and sequence allocation share a row lock with worker completion.
        # 检查会话归属并锁定会话，与 Worker 保存回复时共同保护消息顺序号的分配。
        conversation = session.scalar(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        # A retry must reuse both key and text; a key cannot silently change an accepted message.
        # 重试必须同时复用标识和原文，不能借用已有标识静默修改已接受的消息。
        existing = session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id, Message.submission_key == payload.submission_key
            )
        )
        if existing:
            if existing.content != payload.content:
                raise ConflictError(
                    "IDEMPOTENCY_CONFLICT", "submission_key already used for different content"
                )
            job_id = session.scalar(
                select(Job.id).where(Job.source_message_id == existing.id, Job.kind == "PROCESS_MESSAGE")
            )
            return SubmissionOut(message_id=existing.id, job_id=job_id)
        # Check for a pause after duplicate detection so earlier retries keep their original receipt.
        # 先检查重复提交，再检查审批暂停，使旧请求重试仍能取得原始回执。
        if active_resolution_exists(session, conversation.id):
            raise ConflictError(
                "CONVERSATION_BUSY", "Resolve the active refund proposal before sending a new message."
            )
        # Allocate the sequence and persist the message before creating its foreign-keyed job.
        # 分配顺序号并保存消息，再创建通过外键引用它的任务；任一步失败都会整体回滚。
        message = Message(
            conversation_id=conversation_id,
            role="user",
            content=payload.content,
            seq=conversation.next_message_seq,
            submission_key=payload.submission_key,
        )
        conversation.next_message_seq += 1
        conversation.updated_at = func.clock_timestamp()
        session.add(message)
        session.flush()
        job = Job(source_message_id=message.id)
        session.add(job)
        session.flush()
        # Only a new submission reaches this point. Record the message before its queued job.
        # 只有新提交会到达这里。先记录消息，再记录对应待办入队。
        append_events(session, conversation.id, [message_created_event(message), job_status_event(job)])
        result = SubmissionOut(message_id=message.id, job_id=job.id)
    return result


# Check ownership before returning ordered, paginated history.
# 先检查归属，再返回按顺序分页的历史记录。
@app.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    response_model_exclude_unset=True,
    description=(
        "Read your conversation's messages, saved execution IDs and proposals. The first page (after_seq=0) includes "
        "an X-Conversation-Event-Cursor header for durable SSE. Use after_seq and limit for pagination."
    ),
    responses={
        200: {
            "headers": {
                "X-Conversation-Event-Cursor": {
                    "description": "Durable SSE cursor from the first page's database snapshot.",
                    "schema": {"type": "string", "example": "v2:42"},
                }
            }
        }
    },
)
def list_messages(
    conversation_id: UUID,
    response: Response,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Return the owned message page and attach a durable cursor to the first page.
    返回当前账号的消息页，并为首屏附加持久化事件游标。
    """
    first_page = after_seq == 0
    if first_page:
        # Configure isolation before the first SQL statement on this request session.
        # 在请求会话的第一条 SQL 执行前设置隔离级别。
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    if first_page:
        # Read the page and its cursor in the same snapshot so events cannot slip between them.
        # 在同一快照中读出首屏与游标，防止遗漏两次读取之间提交的事件。
        messages, seq = read_history_snapshot(session, conversation_id, limit)
        if seq is None:
            raise HTTPException(503, "Conversation events temporarily unavailable")
        response.headers["X-Conversation-Event-Cursor"] = durable_cursor(seq)
        return messages
    return read_history(session, conversation_id, after_seq, limit)


@app.get(
    "/executions/{execution_id}",
    response_model=ExecutionDetailOut,
    description="Read the saved eligibility, approval and simulated refund result for an execution you own.",
    responses={404: {"description": "Execution not found"}},
)
def get_execution(
    execution_id: UUID,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> ExecutionDetailOut:
    detail = read_execution_detail(session, execution_id, user.id)
    if detail is None:
        raise HTTPException(404, "Execution not found")
    return detail


# Check ownership once, then stream committed changes for that conversation.
# 先检查一次会话归属，再推送该会话中已提交的变化。
@app.get(
    "/conversations/{conversation_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    description=(
        "Stream your conversation's new messages, job status changes and proposal status changes as "
        "Server-Sent Events. Send Last-Event-ID with the last message sequence received to replay later messages."
        " With version=2, provide a v2:<sequence> cursor to replay committed conversation events."
    ),
)
async def stream_events(
    conversation_id: UUID,
    request: Request,
    version: Literal["1", "2"] = "1",
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    user: User = Depends(current_user),
):
    """Authorize the stream in a short session; each poll later opens its own read-only session.
    在短会话中完成授权；之后每次轮询各自打开只读会话，流期间不长期占用数据库连接。
    """

    def open_stream() -> ConversationStream | DurableConversationStream:
        """Check ownership and validate the cursor before returning a stream reader.
        校验会话归属和游标后，再返回事件流读取器。
        """
        with SessionLocal() as session:
            owned = session.scalar(
                select(Conversation.id).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
            if owned is None:
                raise HTTPException(404, "Conversation not found")
            if version == "1":
                return ConversationStream(conversation_id, parse_last_event_id(last_event_id))

            # Check the versioned cursor against the committed head of this conversation.
            # 将新版游标与当前会话已提交的最新事件序号比较。
            seq = parse_durable_cursor(last_event_id)
            if seq is None:
                raise HTTPException(422, "Invalid conversation event cursor")
            head = read_event_head(session, conversation_id)
            if head is None:
                raise HTTPException(503, "Conversation events temporarily unavailable")
            if seq > head:
                raise HTTPException(422, "Conversation event cursor is ahead of the stream")
            return DurableConversationStream(conversation_id, seq)

    stream = await run_in_threadpool(open_stream)
    token_hash = getattr(request.state, "session_hash", None)
    return StreamingResponse(
        conversation_events(
            stream,
            request.is_disconnected,
            (lambda: session_user(token_hash) is not None) if token_hash else None,
        ),
        media_type="text/event-stream",
        # Disable caching and proxy buffering so each frame reaches the browser promptly.
        # 禁用缓存与代理缓冲，使每个事件帧及时到达浏览器。
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post(
    "/proposals/{proposal_id}/decision",
    response_model=ProposalDecisionOut,
    responses={409: {"model": ConflictOut, "description": "Proposal decision conflict"}},
)
def decide_proposal(
    proposal_id: UUID,
    payload: ProposalDecisionIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Persist a human decision, its events and any approved continuation atomically.
    原子保存人工决定、对应事件和获批的恢复待办。
    """
    with session.begin():
        # Serialize decisions with message submissions and final replies in the same conversation.
        # 将审批与同一会话的消息提交、最终回复串行化。
        row = session.execute(
            select(Conversation, ResolutionProposal, Message.seq)
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Execution, Execution.source_message_id == Message.id)
            .join(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
            .where(
                ResolutionProposal.id == proposal_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update(of=Conversation)
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Proposal not found")
        conversation, proposal, source_seq = row
        if payload.version != PROPOSAL_VERSION:
            raise ConflictError("PROPOSAL_VERSION_CONFLICT", "Proposal version has changed")
        execution = session.get(Execution, proposal.execution_id, with_for_update=True)

        # Look up the decision after locking so concurrent retries see the committed receipt.
        # 加锁后读取决定，使并发重试能看到已提交的审批回执。
        existing = session.get(ProposalDecision, proposal.id)
        if existing is not None:
            if existing.decision != payload.decision:
                raise ConflictError("DECISION_CONFLICT", "A different decision has already been recorded")
            return ProposalDecisionOut.model_validate(existing)
        if execution.status != "WAITING_FOR_APPROVAL":
            code: ConflictCode = "PROPOSAL_NOT_PENDING"
            if (
                execution.status == "INVALIDATED"
                and read_invalidation_reason(session, conversation.id, proposal.id) == "EXPIRED"
            ):
                code = "PROPOSAL_EXPIRED"
            raise ConflictError(code, "Proposal is no longer waiting for approval")
        # Upgraded conversations can contain several pending proposals from the planning-only release.
        # 从仅支持准备方案的版本升级时，同一会话可能保留多个待审批方案。
        if session.scalar(select(unfinished_turn_before(conversation.id, source_seq))):
            raise ConflictError(
                "CONVERSATION_BUSY",
                "Resolve earlier turns in this conversation before deciding this proposal",
            )
        now = session.scalar(select(func.clock_timestamp()))
        if now >= proposal.expires_at:
            events = invalidate_proposal(
                session, conversation, execution, proposal.id, "EXPIRED", EXPIRED_REPLY
            )
            append_events(session, conversation.id, events)
        else:
            # Only approval queues work. Rejection records a final reply without business writes.
            # 只有批准才入队。拒绝仅保存最终回复，不写入业务数据。
            job = None
            if payload.decision == "APPROVE":
                job = Job(source_message_id=execution.source_message_id, kind="RESUME_EXECUTION")
                session.add(job)
                session.flush([job])
                execution.status = "APPROVED"
                conversation.updated_at = now
                followup = job_status_event(job)
            else:
                message = close_proposal(
                    session, conversation, execution, "REJECTED", "The refund proposal was rejected."
                )
                followup = message_created_event(message)
            decision = ProposalDecision(
                proposal_id=proposal.id,
                user_id=user.id,
                decision=payload.decision,
                decided_at=now,
                resume_job_id=job.id if job else None,
            )
            session.add(decision)
            # The decision precedes the continuation or final reply in the event log.
            # 在事件日志中，先记录决定，再记录恢复待办或最终回复。
            decision_event = EventToAppend(
                "proposal.decided",
                {
                    "proposal_id": str(proposal.id),
                    "execution_id": str(execution.id),
                    "source_message_id": str(execution.source_message_id),
                    "decision": decision.decision,
                    "status": "QUEUED" if job else "REJECTED",
                    "resume_job_id": str(job.id) if job else None,
                },
            )
            append_events(session, conversation.id, [decision_event, followup])
            return ProposalDecisionOut.model_validate(decision)

    # Commit the expiration and its reply before returning the conflict to the client.
    # 先提交失效状态及其回复，再向客户端返回冲突。
    raise ConflictError("PROPOSAL_EXPIRED", "Proposal has expired")


# Follow the source message to enforce conversation ownership.
# 通过来源消息检查会话归属，限制待办访问。
@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    description="Read the processing status of a job belonging to your account.",
)
def get_job(job_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Resolve job ownership through its source message and expose the public job schema.
    通过来源消息和会话确认任务归属，再由公开响应模型过滤掉领取令牌等内部字段。
    """
    job = session.scalar(
        select(Job)
        .join(Message, Job.source_message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Job.id == job_id, Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
    )
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/jobs/{job_id}/steps", response_model=list[AgentStepOut])
def get_agent_steps(
    job_id: UUID,
    after_seq: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Expose ordered saved progress after checking the same ownership as job reads.
    按待办读取相同的归属规则，返回有序的已保存进度。
    """
    get_job(job_id, user, session)
    return session.scalars(
        select(AgentStep)
        .where(AgentStep.job_id == job_id, AgentStep.seq > after_seq, AgentStep.finished_at.is_not(None))
        .order_by(AgentStep.seq)
        .limit(100)
    ).all()
