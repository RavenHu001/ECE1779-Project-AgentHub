# Define the public API contract; runtime defaults and validation messages are English.
# 定义公开接口契约；运行时默认内容和校验提示使用英文。

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


# Expose conversation metadata without internal sequence-allocation fields.
# 返回会话资料，不公开内部顺序号分配字段。
class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
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
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


# Return saved chat text and its relationship to the originating request.
# 返回已保存的聊天文本，以及与原始请求的关联。
class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant"]
    content: str
    seq: int
    reply_to_message_id: UUID | None
    created_at: datetime


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
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]
    attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
