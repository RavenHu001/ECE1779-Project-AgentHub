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
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import aliased

from app.config import positive_number
from app.db import SessionLocal
from app.models import Conversation, Job, Message
from app.replies import FixedReply, ReplySettings, RetryableReplyError, build_reply_provider
from app.resolution_workflow import (
    append_final_reply,
    apply_approved_resolution,
    expire_pending_proposal,
    unfinished_turn_before,
)
from app.support_workflow import check_demo_refund_request, parse_prepare_refund, prepare_refund

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
    kind: str


# Clear all ownership fields together to satisfy the job-state constraint.
# 一起清空所有领取字段，满足待办状态约束。
def clear_lease(job):
    """Release all ownership fields as one state transition.
    将全部领取字段作为一次状态变更清空，保持数据库状态约束成立。
    """
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None


# Skip rows locked by other workers and reclaim expired attempts safely.
# 跳过其他 worker 已锁定的记录，并安全接管过期工作。
def claim_job(owner: str, lease_seconds=60, max_attempts=3) -> Claim | None:
    """Atomically claim ready work without overtaking an unfinished conversation turn.
    原子领取已就绪的任务，且不越过同一会话中尚未完成的较早消息。
    """
    with SessionLocal.begin() as session:
        # Use database time and skip locked jobs so replicas do not share one claim.
        # 使用数据库时间并跳过已锁定任务，避免多个副本领取同一项工作。
        now = session.scalar(select(func.clock_timestamp()))
        job = session.scalar(
            select(Job)
            .join(Message, Job.source_message_id == Message.id)
            .where(
                # Keep retries and approvals ahead of every later turn in the same conversation.
                # 同一会话中的重试与审批均优先于后续轮次。
                ~unfinished_turn_before(Message.conversation_id, Message.seq),
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
            # Exhausted interrupted work must become terminal to unblock its successors.
            # 中断后已耗尽领取次数的任务必须进入终态，后续消息才能继续处理。
            job.status = "FAILED"
            job.finished_at = now
            job.updated_at = now
            job.last_error = "Worker claim limit reached after interruption"
            clear_lease(job)
            return None
        # Each attempt gets a fresh token; every later write must prove this ownership.
        # 每次尝试使用新令牌；后续每一次写入都必须证明仍持有此次领取权。
        job.status = "RUNNING"
        job.attempts += 1
        job.started_at = job.started_at or now
        job.updated_at = now
        job.lease_owner = owner
        job.lease_token = uuid4()
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return Claim(job.id, job.lease_token, job.kind)


# Use live database time so ownership is checked after any lock wait.
# 使用数据库实时时间，确保锁等待之后仍重新判断领取权。
def owned_job(claim):
    """Build a fencing predicate using the current token and live lease expiry.
    用当前令牌和实时租约到期时间构造写入条件，阻止旧 Worker 提交结果。
    """
    return and_(
        Job.id == claim.job_id,
        Job.status == "RUNNING",
        Job.lease_token == claim.token,
        Job.lease_expires_at > func.clock_timestamp(),
    )


def renew_lease(claim: Claim, lease_seconds=60):
    """Extend an active claim in its own transaction, or report lost ownership.
    在独立事务中延长有效租约；令牌过期或已更换时报告领取权丢失。
    """
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
    """Keep ownership alive while generation runs without holding a database session.
    生成回复期间通过短事务续租，避免在等待外部请求时长期占用数据库会话。
    """
    stop = Event()
    lost = Event()

    def heartbeat():
        """Renew every third of a lease and remember failures for the caller.
        每经过三分之一租期续租一次，并记录失败以便主处理流程拒绝提交。
        """
        while not stop.wait(lease_seconds / 3):
            try:
                renew_lease(claim, lease_seconds)
            except (LostLease, SQLAlchemyError):
                lost.set()
                return

    # Renewal uses a separate thread; the foreground request owns no long transaction.
    # 独立线程负责续租，前台模型请求不持有长事务。
    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
        if lost.is_set():
            raise LostLease()
    finally:
        # Always stop the heartbeat, including provider failures and cancellation paths.
        # 无论模型失败还是处理异常，都要停止心跳，避免任务结束后继续续租。
        stop.set()
        thread.join(timeout=2)


def load_context(claim: Claim, settings: ReplySettings):
    """Load recent model input from this conversation within row and character limits.
    只加载当前会话中不晚于来源消息的历史，并同时限制消息条数与总字符数。
    """
    with SessionLocal() as session:
        # Check ownership and the current message before spending work on history.
        # 读取历史前先确认领取权和当前消息，超限的当前消息直接失败而不是截断。
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
        # 较早问题的回复可能晚于当前问题入库，因此按回复对应的问题顺序排列。
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
        # Keep a contiguous recent suffix, then reverse it into chronological turn order.
        # 从最近消息起保留连续且未超限的历史，再反转为模型需要的问答时间顺序。
        remaining = settings.context_chars
        context = []
        for message, _turn in rows:
            if len(message.content) > remaining:
                break
            context.append({"role": message.role, "content": message.content})
            remaining -= len(message.content)
        return list(reversed(context))


def retry_job(claim: Claim, *, max_attempts):
    """Persist a bounded retry delay, or mark the final attempt as failed.
    将有次数上限的重试延迟写入数据库；最后一次尝试失败时直接进入失败终态。
    """
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
            # attempts includes the first claim; delay grows exponentially up to 30 s.
            # attempts 包含首次领取；重试延迟按指数增长，但最多等待 30 秒。
            job.status = "QUEUED"
            job.available_at = now + timedelta(seconds=min(2**job.attempts, 30))
            job.last_error = "Model temporarily unavailable; retry scheduled"
        clear_lease(job)


# Commit the result and completion together, rechecking ownership before success.
# 一起提交处理结果与完成状态，在标记成功前再次核对领取权。
def complete_job(claim: Claim, reply: str | None = None, *, check_refund_requests: bool = False):
    """Commit a reply, proposal, or approved action together with the fenced job result.
    在同一次有效领取下提交回复、方案或获批动作，以及待办结果。
    """
    # Lock the current job and load the source of the reply before staging writes.
    # 在准备写入前锁定当前待办，并读取回复对应的来源消息。
    with SessionLocal.begin() as session:
        job = session.scalar(select(Job).where(owned_job(claim)).with_for_update())
        if not job:
            raise LostLease()
        source = session.get(Message, job.source_message_id)
        if not source or source.role != "user":
            raise ValueError("Job source must be a user message")
        # Serialize sequence allocation against both incoming messages and other replies.
        # 锁定会话后分配消息顺序号，避免与新用户消息或其他回复争用相同编号。
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == source.conversation_id).with_for_update()
        )
        # Continuations use the saved decision. Initial jobs can only query or prepare.
        # 恢复待办使用已保存的决定，初始待办只能查询或准备方案。
        if job.kind == "RESUME_EXECUTION":
            apply_approved_resolution(session, job, conversation)
        else:
            if reply is None or not reply.strip():
                raise ValueError("Reply must not be blank")
            final_reply = reply
            if check_refund_requests:
                action = parse_prepare_refund(source.content)
                if action is not None:
                    final_reply = prepare_refund(session, source.id, conversation.user_id, action)
                else:
                    final_reply = (
                        check_demo_refund_request(session, source.id, source.content, conversation.user_id)
                        or reply
                    )
            # A pending card reserves the final reply slot for the decision or execution.
            # 待审批卡片为审批决定或执行结果保留最终回复位置。
            if final_reply is not None:
                append_final_reply(session, conversation, source.id, final_reply)
            else:
                conversation.updated_at = func.clock_timestamp()
        session.flush()
        # Recheck live time after lock waits so an expired claim rolls back all its writes.
        # 锁等待后再次按实时时间校验，领取过期时回滚本次写入的所有记录。
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
            # Raising here also rolls back the check result, proposal, reply and sequence.
            # 此处抛出异常，也会回滚资格记录、方案、退款和回复。
            raise LostLease()


# Record a sanitized failure only while this worker still owns the job.
# 仅在仍持有领取权时记录经过简化的错误。
def fail_job(claim: Claim, error: Exception):
    """Record only an exception type, and only while this attempt still owns the job.
    只在仍持有领取权时记录异常类型，避免保存可能含密钥或提供商正文的异常信息。
    """
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
    """Expire a waiting proposal or process one job, including approved continuations.
    处理一份过期的待审批方案或一个待办，包括获批后的恢复执行。
    """
    with SessionLocal.begin() as session:
        if expire_pending_proposal(session):
            return True
    claim = claim_job(owner, lease_seconds, max_attempts)
    if not claim:
        return False
    provider = provider or FixedReply(reply)
    settings = settings or ReplySettings()
    try:
        # Applying a saved decision is independent of model configuration and availability.
        # 执行已保存的决定与模型配置及模型可用性无关。
        if claim.kind == "RESUME_EXECUTION":
            complete_job(claim)
            log.info("job=%s completed resolution continuation", claim.job_id)
            return True
        with maintain_lease(claim, lease_seconds):
            # Fixed replies ignore history and must preserve the API's 20,000-character limit.
            # 固定回复无需历史，必须保留 API 原有的 20,000 字符接收能力。
            context = [] if provider.mode == "fixed" else load_context(claim, settings)
            generated = provider.generate(context)
        complete_job(claim, generated, check_refund_requests=provider.mode == "fixed")
        log.info("job=%s completed mode=%s", claim.job_id, provider.mode)
    except RetryableReplyError:
        # The queue owns retries; a lost lease must not requeue somebody else's attempt.
        # 重试由数据库队列管理；领取权丢失后不能改写其他 Worker 已接管的任务。
        try:
            retry_job(claim, max_attempts=max_attempts)
        except LostLease:
            log.warning("job=%s lost ownership during retry", claim.job_id)
        log.warning("job=%s transient model failure", claim.job_id)
    except LostLease:
        log.warning("job=%s lost ownership; no reply committed", claim.job_id)
    except OperationalError as exc:
        # Let the next owner reclaim an interrupted database transaction after its lease expires.
        # 数据库事务中断时，等待租约到期后由下一次领取接管。
        if claim.kind != "RESUME_EXECUTION":
            fail_job(claim, exc)
        log.warning("job=%s database operation interrupted", claim.job_id)
    except Exception as exc:
        fail_job(claim, exc)
        log.error("job=%s processing failed type=%s", claim.job_id, type(exc).__name__)
    return True


# Poll for work and finish the current bounded operation before shutdown.
# 轮询待办，并在结束当前短操作后退出。
def main():
    """Validate settings once, then poll until shutdown after the current operation.
    启动时校验配置，随后持续轮询；收到退出信号后完成当前操作再结束进程。
    """
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
        """Request shutdown without interrupting an in-progress database commit.
        只设置停止标志，避免中断正在进行的数据库提交。
        """
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker=%s started mode=%s", owner, provider.mode)
    while not stopping:
        # Idle or unavailable databases back off; completed work immediately frees the loop.
        # 无任务或数据库不可用时等待轮询间隔；处理完任务后立即尝试下一项。
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
