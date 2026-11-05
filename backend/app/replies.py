"""Bounded chat providers. Only the worker creates or calls a model client.
提供带限额的聊天回复生成器，只有独立 Worker 会创建或调用模型客户端。
"""

import os
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from app.config import positive_number

# Keep model instructions English and explicit about business capabilities that do not exist yet.
# 模型指令使用英文，并明确列出当前尚未实现的业务能力，避免声称已执行外部操作。
SYSTEM_PROMPT = """You are AgentHub, a helpful assistant for a small business support workspace.
Answer questions clearly and briefly. Ask for missing information when necessary.
This baseline supports text chat only. You have no access to tickets, orders, refunds,
email, or other business tools. Never claim to have checked data or performed actions.
Explain that business tools and human approval are planned when a request requires them.
"""


class RetryableReplyError(Exception):
    """A sanitized transient failure that the durable queue may retry.
    已去除提供商原始正文的临时故障，由持久队列决定是否重试。
    """


@dataclass(frozen=True)
class ReplySettings:
    """Immutable mode selection and per-request model input/output limits.
    不可变的回复模式与单次模型输入输出限额；这些限制并非账户金额预算。
    """

    mode: str = "fixed"
    model: str = "gpt-5.4-mini"
    timeout: float = 30
    max_output_tokens: int = 600
    context_messages: int = 20
    context_chars: int = 12000

    @classmethod
    def from_env(cls):
        """Parse environment values once and reject unusable settings before polling jobs.
        启动时一次性解析环境变量，并在领取任务之前拒绝不可用的配置。
        """
        # Convert numeric limits explicitly; positive fractions must not become zero-sized limits.
        # 显式转换数值限额；正的小数取整后也不能成为零条消息或零字符的限额。
        settings = cls(
            mode=os.getenv("REPLY_MODE", "fixed"),
            model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            timeout=positive_number("MODEL_TIMEOUT_SECONDS", "30"),
            max_output_tokens=int(positive_number("MODEL_MAX_OUTPUT_TOKENS", "600")),
            context_messages=int(positive_number("MODEL_CONTEXT_MESSAGES", "20")),
            context_chars=int(positive_number("MODEL_CONTEXT_CHARS", "12000")),
        )
        if settings.mode not in {"fixed", "openai"}:
            raise ValueError("REPLY_MODE must be fixed or openai")
        if not settings.model.strip():
            raise ValueError("OPENAI_MODEL must not be blank")
        if min(settings.max_output_tokens, settings.context_messages, settings.context_chars) < 1:
            raise ValueError("Model context and output limits must be at least 1")
        return settings


class FixedReply:
    """Generate deterministic text without using history or an external service.
    生成确定性的固定文本，不使用会话历史，也不访问外部服务。
    """

    mode = "fixed"

    def __init__(self, reply="Message received."):
        """Keep the configured fixed text for every successful message job.
        保存配置的固定文本，供每个成功处理的消息任务返回。
        """
        self.reply = reply

    def generate(self, _messages):
        """Ignore the common provider input; model context limits do not apply here.
        忽略统一生成器接口传入的消息列表，此模式不受模型上下文限额影响。
        """
        return self.reply


class OpenAIReply:
    """Generate bounded text through the Responses API with queue-owned retries.
    通过 Responses API 生成带限额的文本，并把重试控制权交给持久任务队列。
    """

    mode = "openai"

    def __init__(self, settings: ReplySettings, *, client=None):
        """Reuse one worker client, or accept an injected SDK client for offline tests.
        为 Worker 复用一个客户端，也允许测试注入使用离线传输的 SDK 客户端。
        """
        self.settings = settings
        # No SDK retries: the persisted job queue owns the bounded retry policy.
        # 禁用 SDK 自身重试，使实际调用次数统一受数据库任务的尝试次数约束。
        self.client = client or OpenAI(timeout=settings.timeout, max_retries=0)

    def generate(self, messages):
        """Return completed nonblank output, classifying safe retryable and terminal errors.
        只返回已完成且非空的文本，并将模型异常分类为可重试或终止错误。
        """
        try:
            # Send explicit history without tools or stored-response chaining; PostgreSQL owns history.
            # 显式发送历史，不提供工具或依赖远端已保存响应；会话历史由 PostgreSQL 管理。
            result = self.client.responses.create(
                model=self.settings.model,
                instructions=SYSTEM_PROMPT,
                input=messages,
                max_output_tokens=self.settings.max_output_tokens,
                reasoning={"effort": "none"},
                store=False,
            )
        except APIConnectionError:
            # Replace raw transport details so keys and provider content cannot enter public errors.
            # 替换原始传输异常，避免密钥、请求内容或提供商正文进入公开错误。
            raise RetryableReplyError("Model connection unavailable") from None
        except RateLimitError as exc:
            # Retrying an exhausted balance will not help. Other rate limits may clear.
            # 余额耗尽无法通过重试恢复；临时限流则可以由队列稍后重试。
            if exc.code not in {"insufficient_quota", "credit_balance_exhausted"}:
                raise RetryableReplyError("Model rate limited") from None
            raise ValueError("Model account credits unavailable") from None
        except APIStatusError as exc:
            # Server failures, timeouts, and conflicts may recover; other rejected requests stop.
            # 服务端错误、超时与冲突可能恢复；其他明确拒绝的请求直接终止。
            if exc.status_code >= 500 or exc.status_code in {408, 409}:
                raise RetryableReplyError("Model temporarily unavailable") from None
            raise ValueError("Model request rejected") from None
        # Incomplete or empty output is a failure, not a fabricated successful assistant reply.
        # 未完成或空白输出按失败处理，不将其伪装成成功的助手回复。
        if result.status != "completed" or not result.output_text.strip():
            raise ValueError("Model did not produce a complete text reply")
        return result.output_text


def build_reply_provider(settings):
    """Select one provider at startup, failing explicitly if live mode lacks a private key.
    启动时选择回复生成器；真实模型模式缺少私有密钥时明确失败，不静默降级。
    """
    if settings.mode == "openai":
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise ValueError("Set OPENAI_API_KEY privately before enabling openai mode")
        return OpenAIReply(settings)
    reply = os.getenv("FIXED_REPLY", "Message received.")
    if not reply.strip():
        raise ValueError("FIXED_REPLY must not be blank")
    return FixedReply(reply)
