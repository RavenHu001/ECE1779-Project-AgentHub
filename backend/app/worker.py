# Process durable jobs with bounded ownership and a deterministic English reply.
# 以有时限的领取权处理持久待办，并返回固定英文回复。

import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import positive_number
from app.db import SessionLocal
from app.models import Conversation, Job, Message

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
    with SessionLocal.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        job = session.scalar(
            select(Job)
            .where(
                or_(
                    and_(Job.status == "QUEUED", Job.available_at <= now),
                    and_(Job.status == "RUNNING", Job.lease_expires_at <= now),
                )
            )
            .order_by(Job.available_at, Job.created_at)
            .with_for_update(skip_locked=True)
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


# Commit the reply and completion together, rechecking ownership before success.
# 一起提交回复与完成状态，在标记成功前再次核对领取权。
def complete_job(claim: Claim, reply: str):
    if not reply.strip():
        raise ValueError("Fixed reply must not be blank")
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
def run_once(owner="worker", *, lease_seconds=60, max_attempts=3, reply="Message received.") -> bool:
    claim = claim_job(owner, lease_seconds, max_attempts)
    if not claim:
        return False
    try:
        complete_job(claim, reply)
        log.info("job=%s completed mode=fixed", claim.job_id)
    except LostLease:
        log.warning("job=%s lost ownership; no reply committed", claim.job_id)
    except Exception as exc:
        fail_job(claim, exc)
        log.error("job=%s processing failed type=%s", claim.job_id, type(exc).__name__)
    return True


# Poll for work and finish the current short operation before shutdown.
# 轮询待办，并在结束当前短操作后退出。
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll = positive_number("WORKER_POLL_SECONDS", "1")
    lease = positive_number("WORKER_LEASE_SECONDS", "60")
    attempts = int(positive_number("WORKER_MAX_ATTEMPTS", "3"))
    if attempts < 1:
        raise ValueError("WORKER_MAX_ATTEMPTS must be at least 1")
    reply = os.getenv("FIXED_REPLY", "Message received.")
    if not reply.strip():
        raise ValueError("FIXED_REPLY must not be blank")
    owner = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    stopping = False

    # Request graceful shutdown instead of interrupting the completion transaction.
    # 请求平稳退出，不直接中断正在提交的事务。
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker=%s started mode=fixed", owner)
    while not stopping:
        try:
            worked = run_once(owner, lease_seconds=lease, max_attempts=attempts, reply=reply)
        except SQLAlchemyError as exc:
            log.error("Database temporarily unavailable type=%s", type(exc).__name__)
            worked = False
        if not worked:
            time.sleep(poll)


if __name__ == "__main__":
    main()
