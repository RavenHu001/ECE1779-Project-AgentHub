"""Exercise model-job boundaries against real PostgreSQL without paid model calls.
使用真实 PostgreSQL 和模拟模型请求，验证任务边界而不调用付费模型。
"""

import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import OpenAI
from sqlalchemy import func, select

from app.db import SessionLocal, engine
from app.models import Job
from app.replies import OpenAIReply, ReplySettings, RetryableReplyError, build_reply_provider
from app.worker import LostLease, claim_job, complete_job, load_context, renew_lease, run_once


def submit(client, conversation=None, content="Hello"):
    """Create a conversation if needed and submit a distinct logical user message.
    按需创建会话，并使用新的去重标识提交一条独立用户消息。
    """
    conversation = conversation or client.post("/conversations", json={}).json()["id"]
    receipt = client.post(
        f"/conversations/{conversation}/messages",
        json={"content": content, "submission_key": str(uuid4())},
    ).json()
    return conversation, receipt


@pytest.mark.parametrize("length", [12001, 20000])
def test_fixed_reply_preserves_full_api_message_limit(client, length):
    """Fixed mode accepts the API limit even when a message exceeds model context limits.
    验证固定模式可处理 API 上限内的长消息，即使长度已超过默认模型上下文限额。
    """
    # The real HTTP endpoint must accept and persist the full text before the worker runs.
    # 先通过真实 HTTP 接口接收并保存完整文本，再运行 Worker 检查最终持久化结果。
    content = "x" * length
    conversation, receipt = submit(client, content=content)
    assert run_once()
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert [message["content"] for message in messages] == [content, "Message received."]


def test_fixed_reply_does_not_read_model_context(client, monkeypatch):
    """Fixed replies remain independent of history reads and model-only settings.
    验证固定回复不依赖历史查询，也不受仅供模型使用的配置限制影响。
    """
    _, receipt = submit(client)

    def reject_context_read(*_args):
        """Fail if a fixed reply accidentally enters the model-context path.
        固定回复误入模型上下文路径时立即失败，防止长消息兼容性问题再次出现。
        """
        raise AssertionError("Fixed mode must not load model context")

    monkeypatch.setattr("app.worker.load_context", reject_context_read)
    assert run_once(settings=ReplySettings(context_chars=1))
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "content,title",
    [("a" + "😀" * 35, "a" + "😀" * 35), ("  " + "😀" * 80 + "  ", "😀" * 70)],
)
def test_unicode_titles_and_original_messages_are_persisted(client, content, title):
    """Store Unicode-safe browser titles and preserve the original non-BMP message text.
    验证浏览器安全截取的 Unicode 标题能保存，且包含非 BMP 字符的原始消息保持不变。
    """
    # These are the exact outputs asserted by the JavaScript title regression cases.
    # 此处标题与 JavaScript 标题回归场景断言的输出一致，覆盖页面到数据库的字符兼容性。
    created = client.post("/conversations", json={"title": title})
    assert created.status_code == 201
    assert created.json()["title"] == title
    conversation, _ = submit(client, conversation=created.json()["id"], content=content)
    assert run_once()
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert messages[0]["content"] == content
    assert messages[1]["content"] == "Message received."


def test_model_receives_saved_context_and_reply_is_persisted(client):
    """Send earlier turns to a provider and atomically persist its linked final reply.
    验证模型收到已保存的历史轮次，且最终回复与来源消息的关联被一起持久化。
    """
    conversation, _ = submit(client, content="My name is Alice.")
    run_once(reply="Hello Alice.")
    _, receipt = submit(client, conversation, "What is my name?")
    seen = []

    def generate(messages):
        """Capture model input and check that history loading released its connection.
        捕获模型输入，并确认加载历史的数据库连接已在模型调用前释放。
        """
        seen.extend(messages)
        # No connection from context loading may remain checked out during a model call.
        # 模型调用期间，历史加载操作不能继续占用连接池中的数据库连接。
        assert engine.pool.checkedout() == 0
        with SessionLocal.begin() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 2
        return "Your name is Alice."

    assert run_once(provider=SimpleNamespace(mode="test", generate=generate))
    assert [m["content"] for m in seen] == ["My name is Alice.", "Hello Alice.", "What is my name?"]
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert messages[-1]["content"] == "Your name is Alice."
    assert messages[-1]["reply_to_message_id"] == receipt["message_id"]


def test_conversation_jobs_are_serial_but_other_conversations_can_run(client):
    """Serialize one conversation without blocking independent conversations.
    验证同一会话按用户消息顺序处理，而其他会话仍可独立并行领取。
    """
    conversation, _ = submit(client, content="First")
    _, second = submit(client, conversation, "Second")
    other, _ = submit(client, content="Other conversation")
    # A second replica may take the other conversation, but cannot overtake "First".
    # 第二个副本可以处理其他会话，但不能越过当前会话中尚未完成的 First。
    first_claim = claim_job("first")
    other_claim = claim_job("other")
    assert first_claim and other_claim
    assert claim_job("blocked") is None
    assert [m["content"] for m in load_context(first_claim, ReplySettings())] == ["First"]
    complete_job(first_claim, "First answer")
    second_claim = claim_job("second")
    assert str(second_claim.job_id) == second["job_id"]
    assert [m["content"] for m in load_context(second_claim, ReplySettings())] == [
        "First",
        "First answer",
        "Second",
    ]
    complete_job(other_claim, "Other answer")
    assert len(client.get(f"/conversations/{other}/messages").json()) == 2


def test_context_is_bounded_and_never_reads_another_conversation(client):
    """Keep current input within both limits and exclude private neighboring history.
    验证上下文条数与字符数限制同时生效，且不包含其他会话的私有历史。
    """
    conversation, _ = submit(client, content="Old text")
    run_once(reply="Old answer")
    submit(client, content="Private other chat")
    run_once()
    submit(client, conversation, "Current")
    claim = claim_job("bounded")
    assert load_context(claim, ReplySettings(context_chars=7)) == [{"role": "user", "content": "Current"}]
    assert load_context(claim, ReplySettings(context_messages=1)) == [{"role": "user", "content": "Current"}]
    with pytest.raises(ValueError, match="context"):
        load_context(claim, ReplySettings(context_chars=6))


def test_heartbeat_keeps_slow_work_owned(client):
    """Keep a slow provider call owned even after the original lease would expire.
    验证慢模型调用期间租约持续续期，原始租期结束后仍不会被其他 Worker 接管。
    """
    _, receipt = submit(client)

    def generate(_messages):
        """Wait beyond the original lease, then attempt a competing claim.
        等待超过原始租期后尝试竞争领取，用实际领取结果验证心跳有效。
        """
        time.sleep(1.5)
        assert claim_job("competitor", lease_seconds=1) is None
        return "Slow response"

    run_once(lease_seconds=1, provider=SimpleNamespace(mode="slow", generate=generate))
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"


def test_stale_worker_cannot_renew_lease(client):
    """Reject an old claim token after an expired task is reclaimed.
    验证过期任务被重新领取后，旧令牌不能继续续租。
    """
    submit(client)
    old = claim_job("old")
    with SessionLocal.begin() as session:
        session.get(Job, old.job_id).lease_expires_at = func.clock_timestamp() - timedelta(seconds=1)
    new = claim_job("new")
    with pytest.raises(LostLease):
        renew_lease(old)
    renew_lease(new)


def test_transient_failure_uses_persisted_bounded_retries(client):
    """Persist retry timing and terminate after the configured total attempt count.
    验证重试时间写入数据库，且达到包含首次领取在内的总次数上限后终止。
    """
    conversation, receipt = submit(client)

    def generate(_messages):
        """Simulate a transient provider failure without making an external request.
        直接抛出可重试异常模拟临时模型故障，不发送任何外部请求。
        """
        raise RetryableReplyError("do not log provider details or keys")

    provider = SimpleNamespace(mode="unavailable", generate=generate)
    run_once(provider=provider, max_attempts=2)
    job = client.get(f"/jobs/{receipt['job_id']}").json()
    assert job["status"] == "QUEUED" and job["attempts"] == 1
    assert not run_once(provider=provider)
    # Advance only availability instead of sleeping through the persisted backoff.
    # 只提前任务可领取时间，避免测试为等待数据库中的退避间隔而实际休眠。
    with SessionLocal.begin() as session:
        session.get(Job, receipt["job_id"]).available_at = func.clock_timestamp()
    run_once(provider=provider, max_attempts=2)
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "FAILED"
    assert len(client.get(f"/conversations/{conversation}/messages").json()) == 1


def test_provider_error_does_not_expose_secret(client, caplog):
    """Keep provider exception text out of both public job errors and logs.
    验证提供商异常正文不会泄漏到公开任务错误或日志中。
    """
    _, receipt = submit(client)

    def generate(_messages):
        """Inject a recognizable private value to detect accidental disclosure.
        注入可识别的敏感标记，以便检查是否发生意外泄漏。
        """
        raise ValueError("sensitive-provider-value")

    run_once(provider=SimpleNamespace(mode="broken", generate=generate))
    job = client.get(f"/jobs/{receipt['job_id']}").json()
    assert job["status"] == "FAILED"
    assert "sensitive-provider-value" not in job["last_error"] + caplog.text


def model_client(handler):
    """Build the real SDK over an in-memory transport using a non-secret dummy key.
    使用非秘密的测试占位密钥和内存传输创建真实 SDK 客户端，完全避免外部调用。
    """
    return OpenAI(
        api_key="test-not-a-real-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_request_contract_and_response_parsing():
    """Check the real SDK request body and extract a completed text response.
    验证真实 SDK 构造的请求正文，并检查完整文本响应的解析结果。
    """
    import json

    def handler(request):
        """Validate bounded text-only input and return a representative Responses payload.
        检查带限额且不含工具的文本请求，再返回具有代表性的 Responses 响应。
        """
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False and body["max_output_tokens"] == 600
        assert body["input"] == [{"role": "user", "content": "Hi"}]
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.4-mini",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Hello!", "annotations": []}],
                    }
                ],
            },
        )

    with model_client(handler) as client:
        provider = OpenAIReply(ReplySettings(mode="openai"), client=client)
        assert provider.generate([{"role": "user", "content": "Hi"}]) == "Hello!"


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (429, "rate_limit_exceeded", True),
        (503, "server_error", True),
        (429, "insufficient_quota", False),
        (401, "invalid_api_key", False),
    ],
)
def test_model_errors_are_classified_without_paid_calls(status, code, retryable):
    """Separate temporary failures from terminal account or authentication errors.
    验证临时故障可重试，而账户额度或身份认证错误直接失败。
    """

    def handler(_request):
        """Return an SDK-compatible error entirely through the mock HTTP transport.
        完全通过模拟 HTTP 传输返回 SDK 能识别的错误响应。
        """
        return httpx.Response(status, json={"error": {"message": "secret", "code": code, "type": code}})

    with model_client(handler) as client:
        provider = OpenAIReply(ReplySettings(mode="openai"), client=client)
        with pytest.raises(RetryableReplyError if retryable else ValueError):
            provider.generate([{"role": "user", "content": "Hi"}])


def test_model_mode_requires_key_and_does_not_fall_back_silently(monkeypatch):
    """Fail explicitly when live mode has no key instead of silently returning demo text.
    验证真实模型模式缺少密钥时明确失败，不会静默返回演示文本。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_reply_provider(ReplySettings(mode="openai"))


def test_demo_client_is_served_without_exposing_credentials(client):
    """Serve public browser assets while keeping private configuration inaccessible.
    验证浏览器静态资源可公开访问，而私有配置文件不能通过 HTTP 读取。
    """
    response = client.get("/", auth=None)
    assert response.status_code == 200
    assert "AgentHub" in response.text
    assert client.get("/static/chat.js", auth=None).status_code == 200
    assert client.get("/.env", auth=None).status_code == 404
