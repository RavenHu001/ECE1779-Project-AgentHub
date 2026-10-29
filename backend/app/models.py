# Map the four initial tables and enforce their local consistency rules.
# 映射起步阶段的四张表，并约束单条记录内部的一致性。

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Shared ORM metadata used to compare models with migrations.
# 用于对比模型与迁移的共用 ORM 元数据。
class Base(DeclarativeBase):
    pass


# An employee account that owns conversations, not a simulated business customer.
# 拥有会话的员工账号，不是模拟业务中的客户。
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint(
            "username = lower(btrim(username)) AND username ~ '[^[:space:]]'", name="ck_users_username"
        ),
    )
    # Application-generated primary key.
    # 由应用生成的主键。
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Unique normalized login name.
    # 唯一且经过规范化的登录名。
    username: Mapped[str] = mapped_column(Text)
    # Optional name shown to the user.
    # 可选的显示名称。
    display_name: Mapped[str | None] = mapped_column(Text)
    # Salted hash; never a plaintext password.
    # 加盐哈希，不是明文密码。
    password_hash: Mapped[str] = mapped_column(Text)
    # Disabled accounts cannot authenticate.
    # 停用账号不能通过身份验证。
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Creation time as a timezone-aware instant.
    # 包含时区语义的创建时间点。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # Last change time; application code updates it explicitly.
    # 最近修改时间，由应用显式更新。
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


# A user-owned chat with a counter for allocating message order.
# 属于某个用户的聊天，并保存消息顺序号计数器。
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("next_message_seq > 0", name="ck_conversations_seq"),
        CheckConstraint("title ~ '[^[:space:]]'", name="ck_conversations_title"),
        Index("ix_conversations_user_updated", "user_id", text("updated_at DESC"), "id"),
    )
    # Application-generated primary key.
    # 由应用生成的主键。
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Owner account; restrict deletion of referenced users.
    # 所属账号；禁止删除仍被引用的用户。
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # User title or the English default for new conversations.
    # 用户设置的标题，或新会话的英文默认标题。
    title: Mapped[str] = mapped_column(Text, server_default=text("'New conversation'"))
    # Next sequence number, allocated while holding the conversation lock.
    # 下一个顺序号，在持有会话锁时分配。
    next_message_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    # Creation time as a timezone-aware instant.
    # 包含时区语义的创建时间点。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # Last change time; application code updates it explicitly.
    # 最近修改时间，由应用显式更新。
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


# A user request or its single final assistant reply.
# 一条用户请求，或其唯一的最终系统回复。
class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        UniqueConstraint("conversation_id", "submission_key", name="uq_messages_submission"),
        UniqueConstraint("reply_to_message_id", name="uq_messages_reply"),
        CheckConstraint("seq > 0", name="ck_messages_seq"),
        CheckConstraint("content ~ '[^[:space:]]'", name="ck_messages_content"),
        CheckConstraint(
            "(role = 'user' AND submission_key IS NOT NULL AND reply_to_message_id IS NULL) OR "
            "(role = 'assistant' AND submission_key IS NULL AND reply_to_message_id IS NOT NULL)",
            name="ck_messages_role_fields",
        ),
    )
    # Application-generated primary key.
    # 由应用生成的主键。
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Conversation containing this message.
    # 这条消息所属的会话。
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="RESTRICT"))
    # Only user and assistant roles are valid at this stage.
    # 本阶段仅允许 user 和 assistant 两种角色。
    role: Mapped[str] = mapped_column(Text)
    # Original text, including valid Unicode input.
    # 原始文本，允许合法的 Unicode 输入。
    content: Mapped[str] = mapped_column(Text)
    # Positive ordering number unique within a conversation.
    # 会话内唯一的正整数顺序号。
    seq: Mapped[int] = mapped_column(BigInteger)
    # Source user message; unique for a final reply.
    # 回复对应的用户消息；一条来源最多有一条最终回复。
    reply_to_message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="RESTRICT"))
    # Client retry identifier; null for assistant replies.
    # 客户端重试标识；系统回复时为空。
    submission_key: Mapped[UUID | None]
    # Creation time as a timezone-aware instant.
    # 包含时区语义的创建时间点。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


# A durable processing item whose state and ownership fields must agree.
# 一项持久处理工作，其状态必须与领取字段保持一致。
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_jobs_source"),
        CheckConstraint("kind = 'PROCESS_MESSAGE'", name="ck_jobs_kind"),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        CheckConstraint(
            "(status = 'QUEUED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL AND attempts >= 1 AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL AND last_error IS NULL AND attempts >= 1) OR "
            "(status = 'FAILED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL AND last_error ~ '[^[:space:]]' AND last_error IS NOT NULL AND attempts >= 1)",
            name="ck_jobs_state",
        ),
        Index("ix_jobs_ready", "status", "available_at", "created_at"),
        Index("ix_jobs_expired", "lease_expires_at", postgresql_where=text("status = 'RUNNING'")),
    )
    # Application-generated primary key.
    # 由应用生成的主键。
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # One processing job per user message in the current milestone.
    # 当前阶段每条用户消息只对应一项处理待办。
    source_message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id", ondelete="RESTRICT"))
    # Only PROCESS_MESSAGE is implemented.
    # 当前仅实现 PROCESS_MESSAGE 工作类型。
    kind: Mapped[str] = mapped_column(Text, server_default=text("'PROCESS_MESSAGE'"))
    # QUEUED, RUNNING, SUCCEEDED or FAILED.
    # 等待、处理中、成功或失败。
    status: Mapped[str] = mapped_column(Text, server_default=text("'QUEUED'"))
    # Number of claims, including recovery attempts.
    # 领取次数，包含接管重试。
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Earliest time a queued job may be claimed.
    # 排队工作最早可以被领取的时间。
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # Sanitized failure summary without credentials.
    # 不包含凭据的简化错误摘要。
    last_error: Mapped[str | None] = mapped_column(Text)
    # Creation time as a timezone-aware instant.
    # 包含时区语义的创建时间点。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # Last change time; application code updates it explicitly.
    # 最近修改时间，由应用显式更新。
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # First claim time, retained across recovery attempts.
    # 首次领取时间，接管重试时保留。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Completion time, null while queued or running.
    # 结束时间；排队或处理中为空。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Identifier of the current worker instance.
    # 当前 worker 实例的标识。
    lease_owner: Mapped[str | None] = mapped_column(Text)
    # Fresh token per claim; old tokens cannot complete the job.
    # 每次领取生成新令牌，旧令牌不能完成工作。
    lease_token: Mapped[UUID | None]
    # Ownership deadline checked using live database time.
    # 使用数据库实时时间检查的领取截止时间。
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
