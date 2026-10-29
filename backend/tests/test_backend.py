# Verify API behavior, ownership, transactional writes and worker recovery.
# 验证接口行为、访问归属、事务写入和 worker 恢复。

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import SessionLocal, engine
from app.models import Base, Conversation, Job, Message, User
from app.worker import LostLease, claim_job, complete_job, run_once


def submit(client, content="Hello", key=None, conversation=None):
    conversation = conversation or client.post("/conversations", json={}).json()["id"]
    payload = {"content": content, "submission_key": str(key or uuid4())}
    response = client.post(f"/conversations/{conversation}/messages", json=payload)
    assert response.status_code == 202, response.text
    return conversation, payload, response.json()


def expire(job_id):
    with SessionLocal.begin() as session:
        job = session.get(Job, job_id)
        job.lease_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)


def test_health_and_authentication(client):
    assert client.get("/health", auth=None).json()["database"] == "ok"
    assert client.get("/conversations", auth=None).status_code == 401
    assert client.get("/me", auth=("alice", "wrong")).status_code == 401
    assert "password_hash" not in client.get("/me").json()
    with SessionLocal.begin() as session:
        session.scalar(select(User).where(User.username == "alice")).is_active = False
    assert client.get("/conversations").status_code == 401


def test_fixed_reply_pipeline_and_pagination(client):
    conversation, _, receipt = submit(client)
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "QUEUED"
    assert run_once("test-worker")
    assert not run_once("test-worker")
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert [m["seq"] for m in messages] == [1, 2]
    assert messages[1]["reply_to_message_id"] == receipt["message_id"]
    assert messages[1]["content"] == "Message received."
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"
    assert len(client.get(f"/conversations/{conversation}/messages?after_seq=1&limit=1").json()) == 1


# English system defaults must not translate or reject valid Unicode user input.
# 英文系统默认内容不能改写或拒绝合法的 Unicode 用户输入。
def test_english_defaults_preserve_unicode_input(client):
    created = client.post("/conversations", json={}).json()
    assert created["title"] == "New conversation"
    conversation, _, _ = submit(client, content="你好，团队", conversation=created["id"])
    assert run_once()
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert messages[0]["content"] == "你好，团队"
    assert messages[1]["content"] == "Message received."
    schema = client.get("/openapi.json").json()
    assert (
        schema["components"]["schemas"]["ConversationCreate"]["properties"]["title"]["default"]
        == "New conversation"
    )


# Upgrading an already-used database changes defaults without rewriting user history.
# 已使用数据库升级时只修改默认值，不重写用户历史。
def test_english_title_migration_preserves_existing_rows(client):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    original = client.post("/conversations", json={"title": "团队讨论"}).json()
    command.downgrade(cfg, "0001_initial")
    with SessionLocal.begin() as session:
        owner = session.scalar(select(User).where(User.username == "alice"))
        legacy = Conversation(user_id=owner.id)
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id
        assert legacy.title == "新对话"
    command.upgrade(cfg, "head")
    with SessionLocal.begin() as session:
        assert session.get(Conversation, original["id"]).title == "团队讨论"
        assert session.get(Conversation, legacy_id).title == "新对话"
        owner = session.scalar(select(User).where(User.username == "alice"))
        fresh = Conversation(user_id=owner.id)
        session.add(fresh)
        session.flush()
        assert fresh.title == "New conversation"


def test_duplicate_submission_and_conflicting_content(client):
    conversation, payload, receipt = submit(client)
    url = f"/conversations/{conversation}/messages"
    assert client.post(url, json=payload).json() == receipt
    assert client.post(url, json={**payload, "content": "different"}).status_code == 409
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.get(Conversation, conversation).next_message_seq == 2


def test_concurrent_duplicate_requests_create_one_job(client):
    conversation = client.post("/conversations", json={}).json()["id"]
    payload = {"content": "hello", "submission_key": str(uuid4())}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: client.post(f"/conversations/{conversation}/messages", json=payload), range(4))
        )
    assert all(response.status_code == 202 for response in results)
    assert len({response.json()["job_id"] for response in results}) == 1


def test_other_users_cannot_read_or_write_records(client):
    conversation, payload, receipt = submit(client)
    bob = ("bob", "bob-test-password")
    assert client.get(f"/conversations/{conversation}/messages", auth=bob).status_code == 404
    assert client.post(f"/conversations/{conversation}/messages", json=payload, auth=bob).status_code == 404
    assert client.get(f"/jobs/{receipt['job_id']}", auth=bob).status_code == 404
    assert client.get("/conversations", auth=bob).json() == []


@pytest.mark.parametrize("content", ["", " ", "\t\n", "\u3000"])
def test_blank_content_rejected(client, content):
    conversation = client.post("/conversations", json={}).json()["id"]
    response = client.post(
        f"/conversations/{conversation}/messages", json={"content": content, "submission_key": str(uuid4())}
    )
    assert response.status_code == 422


def test_message_and_job_rollback_together(client):
    conversation = client.post("/conversations", json={}).json()["id"]

    def fail_job_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO jobs"):
            raise SQLAlchemyError("simulated job write failure")

    event.listen(engine, "before_cursor_execute", fail_job_insert)
    try:
        response = client.post(
            f"/conversations/{conversation}/messages",
            json={"content": "hello", "submission_key": str(uuid4())},
        )
        assert response.status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", fail_job_insert)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert session.get(Conversation, conversation).next_message_seq == 1


def test_concurrent_workers_only_one_claims_work(client):
    submit(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(claim_job, ["w1", "w2", "w3", "w4"]))
    assert len([claim for claim in claims if claim]) == 1


def test_expired_claim_recovered_and_old_worker_fenced(client):
    conversation, _, receipt = submit(client)
    old = claim_job("old")
    expire(old.job_id)
    new = claim_job("new")
    assert new.job_id == old.job_id and new.token != old.token
    with pytest.raises(LostLease):
        complete_job(old, "obsolete reply")
    complete_job(new, "replacement reply")
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert len(messages) == 2 and messages[-1]["content"] == "replacement reply"
    assert client.get(f"/jobs/{receipt['job_id']}").json()["attempts"] == 2


def test_reply_rolls_back_when_completion_update_fails(client):
    conversation, _, _ = submit(client)
    claim = claim_job("worker")

    def fail_final_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE jobs"):
            raise SQLAlchemyError("simulated completion failure")

    event.listen(engine, "before_cursor_execute", fail_final_update)
    try:
        with pytest.raises(SQLAlchemyError):
            complete_job(claim, "must not persist")
    finally:
        event.remove(engine, "before_cursor_execute", fail_final_update)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.get(Conversation, conversation).next_message_seq == 2
    complete_job(claim, "committed once")


def test_worker_failure_is_visible(client):
    _, _, receipt = submit(client)
    assert run_once(reply=" ")
    result = client.get(f"/jobs/{receipt['job_id']}").json()
    assert result["status"] == "FAILED" and "ValueError" in result["last_error"]


def test_interrupted_work_eventually_exhausts_claim_limit(client):
    _, _, receipt = submit(client)
    first = claim_job("old", max_attempts=1)
    expire(first.job_id)
    assert claim_job("new", max_attempts=1) is None
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "FAILED"


def test_database_checks_reject_invalid_message_role(client):
    conversation = client.post("/conversations", json={}).json()["id"]
    with pytest.raises(IntegrityError), SessionLocal.begin() as session:
        session.add(Message(conversation_id=conversation, role="user", content="hello", seq=1))
        session.flush()  # User messages require a retry identifier.
        # 用户消息必须提供重试标识。


def test_reconnect_keeps_records(client):
    conversation, _, _ = submit(client)
    engine.dispose()
    assert client.get(f"/conversations/{conversation}/messages").json()[0]["content"] == "Hello"


def test_migration_matches_models_and_roundtrips():
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM users")) == 0
