"""Bounded chat providers. Only the worker creates or calls a model client."""

import os
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from app.config import positive_number

SYSTEM_PROMPT = """You are AgentHub, a helpful assistant for a small business support workspace.
Answer questions clearly and briefly. Ask for missing information when necessary.
This baseline supports text chat only. You have no access to tickets, orders, refunds,
email, or other business tools. Never claim to have checked data or performed actions.
Explain that business tools and human approval are planned when a request requires them.
"""


class RetryableReplyError(Exception):
    """A transient provider failure; safe to retry without logging provider content."""


@dataclass(frozen=True)
class ReplySettings:
    mode: str = "fixed"
    model: str = "gpt-5.4-mini"
    timeout: float = 30
    max_output_tokens: int = 600
    context_messages: int = 20
    context_chars: int = 12000

    @classmethod
    def from_env(cls):
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
    mode = "fixed"

    def __init__(self, reply="Message received."):
        self.reply = reply

    def generate(self, _messages):
        return self.reply


class OpenAIReply:
    mode = "openai"

    def __init__(self, settings: ReplySettings, *, client=None):
        self.settings = settings
        # No SDK retries: the persisted job queue owns the bounded retry policy.
        self.client = client or OpenAI(timeout=settings.timeout, max_retries=0)

    def generate(self, messages):
        try:
            result = self.client.responses.create(
                model=self.settings.model,
                instructions=SYSTEM_PROMPT,
                input=messages,
                max_output_tokens=self.settings.max_output_tokens,
                reasoning={"effort": "none"},
                store=False,
            )
        except APIConnectionError:
            raise RetryableReplyError("Model connection unavailable") from None
        except RateLimitError as exc:
            # Retrying an exhausted balance will not help. Other rate limits may clear.
            if exc.code not in {"insufficient_quota", "credit_balance_exhausted"}:
                raise RetryableReplyError("Model rate limited") from None
            raise ValueError("Model account credits unavailable") from None
        except APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in {408, 409}:
                raise RetryableReplyError("Model temporarily unavailable") from None
            raise ValueError("Model request rejected") from None
        if result.status != "completed" or not result.output_text.strip():
            raise ValueError("Model did not produce a complete text reply")
        return result.output_text


def build_reply_provider(settings):
    if settings.mode == "openai":
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise ValueError("Set OPENAI_API_KEY privately before enabling openai mode")
        return OpenAIReply(settings)
    reply = os.getenv("FIXED_REPLY", "Message received.")
    if not reply.strip():
        raise ValueError("FIXED_REPLY must not be blank")
    return FixedReply(reply)
