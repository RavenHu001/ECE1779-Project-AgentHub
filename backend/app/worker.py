# Process durable chat jobs with renewable ownership and bounded model calls.
# 以可续期的领取权处理持久聊天待办，并限制模型调用。

import logging
import signal
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app.config import positive_number
from app.db import SessionLocal
from app.models import Conversation, Job, Message
from app.replies import FixedReply, ReplySettings, RetryableReplyError, build_reply_provider

log = logging.getLogger(__name__)


# Signal that the current attempt can no longer commit any result.
# 表示本次处理已失去领取权，不能再提交结果。
class LostLease(Exception):
    pass


# Carry the job ID and the unique token for this specific claim.
# 携带待办编号，以及本次领取独有的令牌。
@dataclass(frozen=True)
class Claim:
    job_id: UUID
    token: UUID


# Clear all ownership fields together to satisfy the job-state constraint.
# 一起清空所有领取字段，满足待办状态约束。
def clear_lease(job):
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None


# Skip rows locked by other workers and reclaim expired attempts safely.
# 跳过其他 worker 已锁定的记录，并安全接管过期工作。
def claim_job(owner: str, lease_seconds=60, max_attempts=3) -> Claim | None:
    earlier_job = aliased(Job)
    earlier_message = aliased(Message)
    pending_predecessor = (
        select(earlier_job.id)
        .join(earlier_message, earlier_job.source_message_id == earlier_message.id)
        .where(
            earlier_message.conversation_id == Message.conversation_id,
            earlier_message.seq < Message.seq,
            earlier_job.status.in_(["QUEUED", "RUNNING"]),
        )
        .exists()
    )
    with SessionLocal.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        job = session.scalar(
            select(Job)
            .join(Message, Job.source_message_id == Message.id)
            .where(
                ~pending_predecessor,
                or_(
                    and_(Job.status == "QUEUED", Job.available_at <= now),
                    and_(Job.status == "RUNNING", Job.lease_expires_at <= now),
                ),
            )
            .order_by(Job.available_at, Job.created_at)
            .with_for_update(skip_locked=True, of=Job)
            .limit(1)
        )
        if not job:
            return None
        if job.attempts >= max_attempts:
            job.status = "FAILED"
            job.finished_at = now
            job.updated_at = now
            job.last_error = "Worker claim limit reached after interruption"
            clear_lease(job)
            return None
        job.status = "RUNNING"
        job.attempts += 1
        job.started_at = job.started_at or now
        job.updated_at = now
        job.lease_owner = owner
        job.lease_token = uuid4()
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return Claim(job.id, job.lease_token)


# Use live database time so ownership is checked after any lock wait.
# 使用数据库实时时间，确保锁等待之后仍重新判断领取权。
def owned_job(claim):
    return and_(
        Job.id == claim.job_id,
        Job.status == "RUNNING",
        Job.lease_token == claim.token,
        Job.lease_expires_at > func.clock_timestamp(),
    )


def renew_lease(claim: Claim, lease_seconds=60):
    with SessionLocal.begin() as session:
        changed = session.execute(
            update(Job)
            .where(owned_job(claim))
            .values(
                lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                updated_at=func.clock_timestamp(),
            )
        ).rowcount
        if changed != 1:
            raise LostLease()


@contextmanager
def maintain_lease(claim: Claim, lease_seconds):
    """Renew in short independent transactions while the model request is in flight."""
    stop = Event()
    lost = Event()

    def heartbeat():
        while not stop.wait(lease_seconds / 3):
            try:
                renew_lease(claim, lease_seconds)
            except (LostLease, SQLAlchemyError):
                lost.set()
                return

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
        if lost.is_set():
            raise LostLease()
    finally:
        stop.set()
        thread.join(timeout=2)


def load_context(claim: Claim, settings: ReplySettings):
    """Bound both row count and characters; never include later queued user messages."""
    with SessionLocal() as session:
        job = session.scalar(select(Job).where(owned_job(claim)))
        if not job:
            raise LostLease()
        source = session.get(Message, job.source_message_id)
        if not source or source.role != "user":
            raise ValueError("Job source must be a user message")
        if len(source.content) > settings.context_chars:
            raise ValueError("Message exceeds model context character limit")
        # An earlier reply can have a greater seq than this queued source message.
        # Include replies by their originating user message instead of physical seq.
        origin = aliased(Message)
        rows = session.execute(
            select(Message, func.coalesce(origin.seq, Message.seq).label("turn_seq"))
            .outerjoin(origin, Message.reply_to_message_id == origin.id)
            .where(
                Message.conversation_id == source.conversation_id,
                func.coalesce(origin.seq, Message.seq) <= source.seq,
            )
            .order_by(func.coalesce(origin.seq, Message.seq).desc(), Message.seq.desc())
            .limit(settings.context_messages)
        ).all()
        remaining = settings.context_chars
        context = []
        for message, _turn in rows:
            if len(message.content) > remaining:
                break
            context.append({"role": message.role, "content": message.content})
            remaining -= len(message.content)
        return list(reversed(context))


def retry_job(claim: Claim, *, max_attempts):
    with SessionLocal.begin() as session:
        job = session.scalar(select(Job).where(owned_job(claim)).with_for_update())
        if not job:
            raise LostLease()
        now = session.scalar(select(func.clock_timestamp()))
        job.updated_at = now
        if job.attempts >= max_attempts:
            job.status = "FAILED"
            job.finished_at = now
            job.last_error = "Model temporarily unavailable; retry limit reached"
        else:
            job.status = "QUEUED"
            job.available_at = now + timedelta(seconds=min(2**job.attempts, 30))
            job.last_error = "Model temporarily unavailable; retry scheduled"
        clear_lease(job)


# Commit the reply and completion together, rechecking ownership before success.
# 一起提交回复与完成状态，在标记成功前再次核对领取权。
def complete_job(claim: Claim, reply: str):
    if not reply.strip():
        raise ValueError("Reply must not be blank")
    with SessionLocal.begin() as session:
        job = session.scalar(select(Job).where(owned_job(claim)).with_for_update())
        if not job:
            raise LostLease()
        source = session.get(Message, job.source_message_id)
        if not source or source.role != "user":
            raise ValueError("Job source must be a user message")
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == source.conversation_id).with_for_update()
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=reply,
                seq=conversation.next_message_seq,
                reply_to_message_id=source.id,
            )
        )
        conversation.next_message_seq += 1
        conversation.updated_at = func.clock_timestamp()
        session.flush()
        changed = session.execute(
            update(Job)
            .where(owned_job(claim))
            .values(
                status="SUCCEEDED",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                finished_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
                last_error=None,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            # Raising here also rolls back the reply and conversation sequence.
            # 此处抛出异常，也会回滚回复及会话顺序号。
            raise LostLease()


# Record a sanitized failure only while this worker still owns the job.
# 仅在仍持有领取权时记录经过简化的错误。
def fail_job(claim: Claim, error: Exception):
    with SessionLocal.begin() as session:
        session.execute(
            update(Job)
            .where(owned_job(claim))
            .values(
                status="FAILED",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                finished_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
                last_error=f"Message processing failed ({type(error).__name__})",
            )
        )


# Process at most one claim; stale attempts cannot write a reply.
# 每次最多处理一项领取；过期处理不能写入回复。
def run_once(
    owner="worker",
    *,
    lease_seconds=60,
    max_attempts=3,
    reply="Message received.",
    provider=None,
    settings=None,
) -> bool:
    claim = claim_job(owner, lease_seconds, max_attempts)
    if not claim:
        return False
    provider = provider or FixedReply(reply)
    settings = settings or ReplySettings()
    try:
        with maintain_lease(claim, lease_seconds):
            context = load_context(claim, settings)
            generated = provider.generate(context)
        complete_job(claim, generated)
        log.info("job=%s completed mode=%s", claim.job_id, provider.mode)
    except RetryableReplyError:
        try:
            retry_job(claim, max_attempts=max_attempts)
        except LostLease:
            log.warning("job=%s lost ownership during retry", claim.job_id)
        log.warning("job=%s transient model failure", claim.job_id)
    except LostLease:
        log.warning("job=%s lost ownership; no reply committed", claim.job_id)
    except Exception as exc:
        fail_job(claim, exc)
        log.error("job=%s processing failed type=%s", claim.job_id, type(exc).__name__)
    return True


# Poll for work and finish the current bounded operation before shutdown.
# 轮询待办，并在结束当前短操作后退出。
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll = positive_number("WORKER_POLL_SECONDS", "1")
    lease = positive_number("WORKER_LEASE_SECONDS", "60")
    attempts = int(positive_number("WORKER_MAX_ATTEMPTS", "3"))
    if attempts < 1:
        raise ValueError("WORKER_MAX_ATTEMPTS must be at least 1")
    settings = ReplySettings.from_env()
    provider = build_reply_provider(settings)
    owner = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    stopping = False

    # Request graceful shutdown instead of interrupting the completion transaction.
    # 请求平稳退出，不直接中断正在提交的事务。
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker=%s started mode=%s", owner, provider.mode)
    while not stopping:
        try:
            worked = run_once(
                owner, lease_seconds=lease, max_attempts=attempts, provider=provider, settings=settings
            )
        except SQLAlchemyError as exc:
            log.error("Database temporarily unavailable type=%s", type(exc).__name__)
            worked = False
        if not worked:
            time.sleep(poll)


if __name__ == "__main__":
    main()
