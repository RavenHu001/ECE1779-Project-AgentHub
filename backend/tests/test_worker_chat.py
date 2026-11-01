"""Exercise model-job boundaries against real PostgreSQL without paid model calls."""

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
    conversation = conversation or client.post("/conversations", json={}).json()["id"]
    receipt = client.post(
        f"/conversations/{conversation}/messages",
        json={"content": content, "submission_key": str(uuid4())},
    ).json()
    return conversation, receipt


def test_model_receives_saved_context_and_reply_is_persisted(client):
    conversation, _ = submit(client, content="My name is Alice.")
    run_once(reply="Hello Alice.")
    _, receipt = submit(client, conversation, "What is my name?")
    seen = []

    def generate(messages):
        seen.extend(messages)
        # No connection from context loading may remain checked out during a model call.
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
    conversation, _ = submit(client, content="First")
    _, second = submit(client, conversation, "Second")
    other, _ = submit(client, content="Other conversation")
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
    _, receipt = submit(client)

    def generate(_messages):
        time.sleep(1.5)
        assert claim_job("competitor", lease_seconds=1) is None
        return "Slow response"

    run_once(lease_seconds=1, provider=SimpleNamespace(mode="slow", generate=generate))
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"


def test_stale_worker_cannot_renew_lease(client):
    submit(client)
    old = claim_job("old")
    with SessionLocal.begin() as session:
        session.get(Job, old.job_id).lease_expires_at = func.clock_timestamp() - timedelta(seconds=1)
    new = claim_job("new")
    with pytest.raises(LostLease):
        renew_lease(old)
    renew_lease(new)


def test_transient_failure_uses_persisted_bounded_retries(client):
    conversation, receipt = submit(client)

    def generate(_messages):
        raise RetryableReplyError("do not log provider details or keys")

    provider = SimpleNamespace(mode="unavailable", generate=generate)
    run_once(provider=provider, max_attempts=2)
    job = client.get(f"/jobs/{receipt['job_id']}").json()
    assert job["status"] == "QUEUED" and job["attempts"] == 1
    assert not run_once(provider=provider)
    with SessionLocal.begin() as session:
        session.get(Job, receipt["job_id"]).available_at = func.clock_timestamp()
    run_once(provider=provider, max_attempts=2)
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "FAILED"
    assert len(client.get(f"/conversations/{conversation}/messages").json()) == 1


def test_provider_error_does_not_expose_secret(client, caplog):
    _, receipt = submit(client)

    def generate(_messages):
        raise ValueError("sensitive-provider-value")

    run_once(provider=SimpleNamespace(mode="broken", generate=generate))
    job = client.get(f"/jobs/{receipt['job_id']}").json()
    assert job["status"] == "FAILED"
    assert "sensitive-provider-value" not in job["last_error"] + caplog.text


def model_client(handler):
    return OpenAI(
        api_key="test-not-a-real-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_request_contract_and_response_parsing():
    import json

    def handler(request):
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
    def handler(_request):
        return httpx.Response(status, json={"error": {"message": "secret", "code": code, "type": code}})

    with model_client(handler) as client:
        provider = OpenAIReply(ReplySettings(mode="openai"), client=client)
        with pytest.raises(RetryableReplyError if retryable else ValueError):
            provider.generate([{"role": "user", "content": "Hi"}])


def test_model_mode_requires_key_and_does_not_fall_back_silently(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_reply_provider(ReplySettings(mode="openai"))


def test_demo_client_is_served_without_exposing_credentials(client):
    response = client.get("/", auth=None)
    assert response.status_code == 200
    assert "AgentHub" in response.text
    assert client.get("/static/chat.js", auth=None).status_code == 200
    assert client.get("/.env", auth=None).status_code == 404
