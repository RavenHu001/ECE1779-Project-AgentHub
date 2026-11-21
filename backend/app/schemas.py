# Define the public API contract; runtime defaults and validation messages are English.
# 定义公开接口契约；运行时默认内容和校验提示使用英文。

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExecutionStatus = Literal[
    "WAITING_FOR_APPROVAL", "QUEUED", "RUNNING", "COMPLETED", "REJECTED", "INVALIDATED", "FAILED"
]
JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]
ConflictCode = Literal[
    "IDEMPOTENCY_CONFLICT",
    "CONVERSATION_BUSY",
    "PROPOSAL_EXPIRED",
    "DECISION_CONFLICT",
    "PROPOSAL_VERSION_CONFLICT",
    "PROPOSAL_NOT_PENDING",
]


class ConflictOut(BaseModel):
    detail: str
    code: ConflictCode


# Reject unknown input fields and provide the English default title.
# 拒绝未知输入字段，并提供英文默认标题。
class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="New conversation", min_length=1, max_length=200)

    # Normalize title spacing after rejecting blank input.
    # 拒绝空白标题后，去掉标题首尾空格。
    @field_validator("title")
    @classmethod
    def check_title(cls, value):
        if "\x00" in value:
            raise ValueError("title must not contain NUL characters")
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


# Expose conversation metadata without internal sequence-allocation fields.
# 返回会话资料，不公开内部顺序号分配字段。
class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_pinned: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Provide a title or pin state; null values are not supported")
        if self.title is not None:
            self.title = ConversationCreate.check_title(self.title)
        return self


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    is_pinned: bool
    created_at: datetime
    updated_at: datetime


# Keep the original text and require a caller-generated retry identifier.
# 保留原始文本，并要求调用方提供用于重试识别的标识。
class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20000)
    submission_key: UUID

    # Reject whitespace-only messages without rewriting valid user content.
    # 拒绝纯空白消息，不改写合法的用户内容。
    @field_validator("content")
    @classmethod
    def check_content(cls, value):
        if "\x00" in value:
            raise ValueError("content must not contain NUL characters")
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


# Expose the persisted proposal without the internal order snapshot used for later checks.
# 返回已保存的方案，隐藏供后续核验使用的内部订单快照。
class ProposalOut(BaseModel):
    id: UUID
    version: Literal[1]
    status: ExecutionStatus
    # Serialize BIGINT identifiers and amounts as strings so browsers preserve every digit.
    # 将 BIGINT 编号和金额序列化为字符串，避免浏览器丢失精度。
    ticket_id: str
    order_id: str
    refund_amount_minor: str
    currency: Literal["CAD"]
    customer_reply: str
    proposed_ticket_status: Literal["RESOLVED"]
    policy_version: str
    delivered_at: datetime
    checked_at: datetime
    expires_at: datetime
    decision: Literal["APPROVE", "REJECT"] | None = None
    decided_at: datetime | None = None
    resume_job_id: UUID | None = None
    revalidated_at: datetime | None = None
    invalidation_reason: str | None = None


# Only the authenticated employee can supply a decision on the identified version.
# 仅经过身份验证的员工可对指定版本作出决定。
class ProposalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]
    version: int = Field(strict=True, gt=0)


# Return the original decision receipt on every identical retry.
# 每次重试相同决定时返回原始审批回执。
class ProposalDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    proposal_id: UUID
    decision: Literal["APPROVE", "REJECT"]
    decided_at: datetime
    resume_job_id: UUID | None


# Expose the persisted business result of an applied simulated refund.
# 公开已执行模拟退款的持久化业务结果。
class AppliedResolutionOut(BaseModel):
    order_id: str
    ticket_id: str
    refund_amount_minor: str
    currency: Literal["CAD"]
    customer_reply: str
    applied_at: datetime


# Return saved eligibility, workflow state and the result for one user turn.
# 返回一轮用户请求已保存的资格判断、流程状态和处置结果。
class ExecutionDetailOut(BaseModel):
    id: UUID
    source_message_id: UUID
    order_id: str
    outcome: Literal["ELIGIBLE", "INELIGIBLE", "NEEDS_INFORMATION", "NOT_FOUND"]
    reason_codes: list[str]
    refund_amount_minor: str | None
    currency: str | None
    policy_version: str
    checked_at: datetime
    status: ExecutionStatus
    proposal_id: UUID | None
    expires_at: datetime | None
    revalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    decision: ProposalDecisionOut | None
    resume_job_status: JobStatus | None
    result: AppliedResolutionOut | None


# Return saved chat text and its relationship to the originating request.
# 返回已保存的聊天文本，以及与原始请求的关联。
class ActivityStepOut(BaseModel):
    seq: int
    name: str
    status: Literal["running", "completed"]
    text: str


class ActivityOut(BaseModel):
    job_id: UUID
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]
    attempts: int
    steps: list[ActivityStepOut]


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant"]
    content: str
    seq: int
    reply_to_message_id: UUID | None
    created_at: datetime
    execution_id: UUID | None = None
    proposal: ProposalOut | None = None
    activity: ActivityOut | None = None


# Return stable identifiers for both a new submission and its retries.
# 为首次提交和重复提交返回稳定的消息与待办编号。
class SubmissionOut(BaseModel):
    message_id: UUID
    job_id: UUID


# Expose progress while omitting worker ownership tokens.
# 提供进度信息，但不公开 worker 的领取令牌。
class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_message_id: UUID
    status: JobStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None


class AgentStepOut(BaseModel):
    """Account-scoped durable progress; worker tokens and raw provider responses stay private.
    按账号授权的持久进度，不公开 worker 令牌或提供商原始响应。
    """

    model_config = ConfigDict(from_attributes=True)
    seq: int
    name: str
    result: dict | None
    created_at: datetime
    finished_at: datetime | None


# Stream job progress with its kind so clients can tell replies from approved continuations.
# 推送任务进度时附带任务类型，便于客户端区分普通回复与批准后的恢复执行。
class JobEventOut(JobOut):
    kind: str


class ConversationEventOut(BaseModel):
    """Describe the committed event as it was recorded at the time of the change.
    描述状态变化发生时已提交的事件内容。
    """

    # Use decimal text to preserve BIGINT sequences in JavaScript clients.
    # 使用十进制字符串，避免 JavaScript 客户端丢失 BIGINT 序号精度。
    seq: str
    type: str
    conversation_id: UUID
    created_at: datetime
    payload: dict
