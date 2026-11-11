# Serve authenticated chat requests; background processing runs in a separate container.
# 提供经过身份验证的聊天接口；后台处理在独立容器运行。

import logging
import os
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import engine, get_session
from app.models import Conversation, Execution, Job, Message, ResolutionProposal, User
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    JobOut,
    MessageCreate,
    MessageOut,
    ProposalOut,
    SubmissionOut,
)
from app.security import current_user

app = FastAPI(
    title="AgentHub Backend", version="0.2.0", description="Durable chat jobs with optional model replies."
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
log = logging.getLogger(__name__)


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
def me(user: User = Depends(current_user)):
    """Return only the authenticated account's public identity fields.
    仅返回已验证账号的公开身份字段，不包含密码哈希或其他认证信息。
    """
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
    """Create a conversation owned by the authenticated user in one short transaction.
    在短事务中创建属于已验证用户的会话，所有权由认证结果确定。
    """
    with session.begin():
        conversation = Conversation(user_id=user.id, title=payload.title)
        session.add(conversation)
        session.flush()
    return conversation


# Restrict the list to the current account and bound its size.
# 只列出当前账号的会话，并限制返回数量。
@app.get(
    "/conversations",
    response_model=list[ConversationOut],
    description="List your conversations, most recently updated first.",
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
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
        .limit(limit)
    ).all()


# Lock one conversation and atomically save its message, sequence and job.
# 锁定对应会话，在同一事务内保存消息、顺序号和待办。
@app.post(
    "/conversations/{conversation_id}/messages",
    response_model=SubmissionOut,
    status_code=202,
    description="Save a message and its background job atomically. Reuse submission_key only when retrying the same request.",
)
def submit_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Atomically save one logical message and job, returning stable IDs for identical retries.
    原子保存一条逻辑消息及其任务；相同内容和去重标识的重试始终返回原有编号。
    """
    with session.begin():
        # Ownership and sequence allocation share a row lock with worker completion.
        # 检查会话归属并锁定会话，与 Worker 保存回复时共同保护消息顺序号的分配。
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user.id)
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
                raise HTTPException(409, "submission_key already used for different content")
            job_id = session.scalar(select(Job.id).where(Job.source_message_id == existing.id))
            return SubmissionOut(message_id=existing.id, job_id=job_id)
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
        result = SubmissionOut(message_id=message.id, job_id=job.id)
    return result


# Check ownership before returning ordered, paginated history.
# 先检查归属，再返回按顺序分页的历史记录。
@app.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    response_model_exclude_unset=True,
    description="Read your conversation's messages and any attached proposals. Use after_seq and limit for pagination.",
)
def list_messages(
    conversation_id: UUID,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Read owned message pages with proposals attached to their source messages.
    分页读取当前账号的消息，并在来源消息上附带对应的方案。
    """
    conversation = session.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    # Read proposals with their messages in one snapshot. Both joins are one-to-one.
    # 在同一快照中读取消息与方案，两次连接均为一对一。
    rows = session.execute(
        select(Message, Execution, ResolutionProposal)
        .outerjoin(Execution, Execution.source_message_id == Message.id)
        .outerjoin(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .where(Message.conversation_id == conversation_id, Message.seq > after_seq)
        .order_by(Message.seq)
        .limit(limit)
    )
    history = []
    for message, execution, proposal in rows:
        result = MessageOut.model_validate(message)
        # Keep ordinary history in its original shape and expose only public proposal fields.
        # 保留普通历史原有的结构，并只返回方案的公开字段。
        if proposal is not None:
            result.proposal = ProposalOut(
                id=proposal.id,
                version=1,
                status=execution.status,
                ticket_id=str(proposal.ticket_id),
                order_id=str(execution.requested_order_id),
                refund_amount_minor=str(execution.refund_amount_minor),
                currency=execution.currency,
                customer_reply=proposal.customer_reply,
                proposed_ticket_status=proposal.proposed_ticket_status,
                policy_version=execution.policy_version,
                delivered_at=proposal.expected_order_facts["delivered_at"],
                checked_at=execution.checked_at,
                expires_at=proposal.expires_at,
            )
        history.append(result)
    return history


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
        .where(Job.id == job_id, Conversation.user_id == user.id)
    )
    if not job:
        raise HTTPException(404, "Job not found")
    return job
