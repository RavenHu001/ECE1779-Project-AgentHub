# Stream legacy updates or replay committed conversation events over SSE.
# 通过 SSE 推送旧版状态更新，或回放已提交的会话事件。

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.activity import read_activity
from app.db import SessionLocal
from app.event_store import MAX_EVENT_PAGE_SIZE, MAX_EVENT_SEQ, read_events
from app.history import read_history, read_proposals
from app.models import Conversation, Job, Message
from app.schemas import ConversationEventOut, JobEventOut

log = logging.getLogger(__name__)

# Tunables are module attributes so tests can shorten them without new settings.
# 参数定义为模块属性，测试可以直接缩短，无需新增配置项。
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 15.0
# Bounded streams let load balancers rebalance and let clients resume with Last-Event-ID.
# 限制单个流的时长，便于负载均衡重新分配连接，客户端随后用 Last-Event-ID 续传。
MAX_STREAM_SECONDS = 300.0
# Rows committed slightly after their timestamp was taken must still be seen by the next poll.
# 时间戳取得后稍晚才提交的行，仍需在下一次查询中被看到。
JOB_LOOKBACK = timedelta(seconds=5)
REPLAY_PAGE = MAX_EVENT_PAGE_SIZE
ACTIVE_JOB_STATES = ("QUEUED", "RUNNING")
LAST_EVENT_ID = re.compile(r"[0-9]{1,19}")
DURABLE_EVENT_ID = re.compile(r"v2:([0-9]{1,19})")


class ConversationRemoved(Exception):
    """Stop existing streams when the owner removes a settled chat.
    所有者移除已结束的聊天后，停止已有事件流。
    """


def check_conversation_visible(session, conversation_id):
    if (
        session.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id, Conversation.deleted_at.is_(None)
            )
        )
        is None
    ):
        raise ConversationRemoved()


def encode(event: str, data: str, event_id: int | str | None = None) -> str:
    """Format one SSE frame; JSON data never contains raw newlines.
    格式化单个 SSE 帧；JSON 数据中不会出现原始换行符。
    """
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


def parse_last_event_id(value: str | None) -> int | None:
    """Accept a non-negative ASCII message sequence within BIGINT range and ignore anything else.
    只接受 BIGINT 范围内、由 ASCII 数字组成的非负消息顺序号，其他值一律按新连接处理。
    """
    # str.isdigit() also accepts characters such as "²" that int() rejects, so match ASCII explicitly.
    # str.isdigit() 也接受 "²" 等 int() 无法转换的字符，因此显式只匹配 ASCII 数字。
    if value is None or not LAST_EVENT_ID.fullmatch(value.strip()):
        return None
    seq = int(value.strip())
    return seq if seq <= MAX_EVENT_SEQ else None


def durable_cursor(seq: int) -> str:
    """Format a durable event cursor distinct from the legacy numeric message cursor.
    为持久化事件生成带版本标记的游标，与旧版数字消息游标区分。
    """
    return f"v2:{seq}"


def parse_durable_cursor(value: str | None) -> int | None:
    """Read the versioned cursor without reinterpreting a legacy message sequence.
    读取带版本标记的游标，避免将旧版消息序号误作事件序号。
    """
    match = DURABLE_EVENT_ID.fullmatch(value.strip()) if value is not None else None
    return parse_last_event_id(match[1]) if match is not None else None


class ConversationStream:
    """Track what one connection has sent and emit only differences on each poll.
    记录单个连接已发送的内容，每次轮询只推送差异部分。
    """

    def __init__(self, conversation_id: UUID, last_seq: int | None):
        self.conversation_id = conversation_id
        self.last_seq = last_seq
        self.job_states: dict[UUID, tuple[str, int]] = {}
        self.activity_states: dict[str, str] = {}
        self.proposal_states: dict[UUID, tuple[str, str | None]] | None = None
        self.since = None

    def poll(self) -> list[str]:
        """Read new messages, job changes and proposal changes in one short read-only session.
        在一个短暂的只读会话中读取新消息、任务变化与方案变化。
        """
        frames: list[str] = []
        with SessionLocal() as session:
            check_conversation_visible(session, self.conversation_id)
            now = session.scalar(select(func.clock_timestamp()))
            if self.last_seq is None:
                # A fresh connection starts at the current end; clients load earlier history over REST.
                # 新连接从当前末尾开始；更早的历史由客户端通过 REST 接口读取。
                self.last_seq = session.scalar(
                    select(func.coalesce(func.max(Message.seq), 0)).where(
                        Message.conversation_id == self.conversation_id
                    )
                )
            if self.since is None:
                frames.append(encode("ready", json.dumps({"cursor": self.last_seq}), self.last_seq))

            # Replay or forward messages in sequence order; the id is the resume cursor.
            # 按顺序号补发或推送消息；事件 id 即续传游标。
            while True:
                page = read_history(session, self.conversation_id, self.last_seq, REPLAY_PAGE)
                for message in page:
                    frames.append(encode("message", message.model_dump_json(exclude_unset=True), message.seq))
                    self.last_seq = message.seq
                if len(page) < REPLAY_PAGE:
                    break

            # Active jobs and recently changed jobs; duplicates are filtered by the tracked state.
            # 读取进行中及最近变化的任务；重复状态由已记录的状态过滤。
            since = (self.since or now) - JOB_LOOKBACK
            jobs = session.scalars(
                select(Job)
                .join(Message, Job.source_message_id == Message.id)
                .where(
                    Message.conversation_id == self.conversation_id,
                    or_(Job.status.in_(ACTIVE_JOB_STATES), Job.updated_at >= since),
                )
                .order_by(Job.created_at, Job.id)
            ).all()
            for job in jobs:
                state = (job.status, job.attempts)
                if self.job_states.get(job.id) != state:
                    self.job_states[job.id] = state
                    frames.append(encode("job", JobEventOut.model_validate(job).model_dump_json()))

            # Publish changed snapshots; reconnects also load persisted activity through history.
            # 推送变化的快照，重连时也通过历史接口恢复持久化进度。
            for source_id, activity in read_activity(
                session, [job.source_message_id for job in jobs]
            ).items():
                data = json.dumps({"source_message_id": str(source_id), **activity})
                if self.activity_states.get(activity["job_id"]) != data:
                    self.activity_states[activity["job_id"]] = data
                    frames.append(encode("activity", data))

            # Proposals change status in place, so compare them against the connection baseline.
            # 方案状态在原记录上变化，因此与连接建立时的基线进行比较。
            proposals = read_proposals(session, self.conversation_id)
            current = {proposal.id: (proposal.status, proposal.decision) for proposal in proposals}
            if self.proposal_states is not None:
                for proposal in proposals:
                    if self.proposal_states.get(proposal.id) != current[proposal.id]:
                        frames.append(encode("proposal", proposal.model_dump_json()))
            self.proposal_states = current
            self.since = now
        return frames


class DurableConversationStream:
    """Read one bounded page from the event log for an authorized conversation.
    为授权会话从事件日志读取一页有数量上限的记录。
    """

    def __init__(self, conversation_id: UUID, last_seq: int):
        """Start from the validated exclusive cursor supplied by the client.
        从客户端提供并已通过校验的排他游标开始回放。
        """
        self.conversation_id = conversation_id
        self.last_seq = last_seq
        self.started = False
        self.page_full = False

    def poll(self) -> list[str]:
        """Emit stored payloads as recorded, then advance the server's next read position.
        原样发出已保存的事件载荷，再推进服务端的读取位置。
        """
        frames: list[str] = []
        # The ready frame confirms the resume point without changing the client's cursor.
        # ready 帧确认续传起点，不改变客户端的游标。
        if not self.started:
            frames.append(encode("ready", json.dumps({"cursor": durable_cursor(self.last_seq)})))
            self.started = True

        # Read only one page per session. A full page can be followed immediately by another read.
        # 每次数据库会话只读一页。读满一页后可以立即继续读取。
        with SessionLocal() as session:
            check_conversation_visible(session, self.conversation_id)
            rows = read_events(session, self.conversation_id, self.last_seq, REPLAY_PAGE)
        for row in rows:
            event = ConversationEventOut(
                seq=str(row.seq),
                type=row.event_type,
                conversation_id=row.conversation_id,
                created_at=row.created_at,
                payload=row.payload,
            )
            frames.append(encode(event.type, event.model_dump_json(), durable_cursor(row.seq)))
            self.last_seq = row.seq
        self.page_full = len(rows) == REPLAY_PAGE
        return frames


async def conversation_events(
    stream: ConversationStream | DurableConversationStream, is_disconnected, is_authorized=None
) -> AsyncIterator[str]:
    """Yield frames until the client leaves, the stream reaches its lifetime, or the database fails.
    持续输出事件帧，直到客户端断开、流达到最长时长或数据库出错。
    """
    started = last_sent = time.monotonic()
    # Suggest a reconnect delay to clients that honour the SSE retry field.
    # 向支持 SSE retry 字段的客户端建议重连间隔。
    yield "retry: 2000\n\n"
    while time.monotonic() - started < MAX_STREAM_SECONDS:
        if await is_disconnected():
            return
        try:
            # Recheck cookie sessions during an open stream, including logout and the absolute expiry.
            # 在流持续期间复查 cookie 会话，包括退出登录和固定到期时间。
            if is_authorized is not None and not await run_in_threadpool(is_authorized):
                return
            frames = await run_in_threadpool(stream.poll)
        except ConversationRemoved:
            yield encode("conversation.deleted", json.dumps({"conversation_id": str(stream.conversation_id)}))
            return
        except SQLAlchemyError as exc:
            # End the stream so the client reconnects; never send SQL details.
            # 结束事件流让客户端重连；不向客户端发送 SQL 细节。
            log.error("Event stream poll failed: %s", type(exc).__name__)
            yield encode("error", json.dumps({"detail": "Database temporarily unavailable"}))
            return
        for frame in frames:
            yield frame
        if frames:
            last_sent = time.monotonic()
        elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
            # Comment lines keep proxies from closing an idle connection.
            # 注释行用于防止代理关闭空闲连接。
            yield ": keepalive\n\n"
            last_sent = time.monotonic()
        # Drain durable backlogs promptly without keeping a long database session.
        # 快速补发持久化积压事件，同时避免长期占用数据库会话。
        if isinstance(stream, DurableConversationStream) and stream.page_full:
            continue
        await asyncio.sleep(POLL_SECONDS)
