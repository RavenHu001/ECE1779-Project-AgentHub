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
from app.models import Conversation, Job, Message, User
from app.schemas import ConversationCreate, ConversationOut, JobOut, MessageCreate, MessageOut, SubmissionOut
from app.security import current_user

app = FastAPI(
    title="AgentHub Backend", version="0.2.0", description="Durable chat jobs with optional model replies."
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
log = logging.getLogger(__name__)


@app.get("/", include_in_schema=False)
def demo():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# Return a safe English error without exposing database parameters.
# 返回安全的英文错误，不公开数据库参数。
@app.exception_handler(SQLAlchemyError)
def database_error(_request: Request, exc: SQLAlchemyError):
    log.error("Database operation failed: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


# Check database connectivity without requiring a user account.
# 无需用户账号即可检查数据库连通性。
@app.get("/health", description="Check API and database availability. Authentication is not required.")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    # This is configured mode, not a claim that the worker/model is healthy.
    return {"status": "ok", "database": "ok", "reply_mode": os.getenv("REPLY_MODE", "fixed")}


# Return the authenticated account without its password hash.
# 返回已验证账号的信息，不返回密码哈希。
@app.get("/me", description="Return the authenticated account without password or credential fields.")
def me(user: User = Depends(current_user)):
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
    with session.begin():
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user.id)
            .with_for_update()
        )
        if not conversation:
            raise HTTPException(404, "Conversation not found")
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
    description="Read your conversation's messages in sequence order. Use after_seq and limit for pagination.",
)
def list_messages(
    conversation_id: UUID,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    conversation = session.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    return session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.seq > after_seq)
        .order_by(Message.seq)
        .limit(limit)
    ).all()


# Follow the source message to enforce conversation ownership.
# 通过来源消息检查会话归属，限制待办访问。
@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    description="Read the processing status of a job belonging to your account.",
)
def get_job(job_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    job = session.scalar(
        select(Job)
        .join(Message, Job.source_message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Job.id == job_id, Conversation.user_id == user.id)
    )
    if not job:
        raise HTTPException(404, "Job not found")
    return job
