# Verify API behavior, ownership, transactional writes and worker recovery.
# 验证接口行为、访问归属、事务写入和 worker 恢复。

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
from app.manage import save_user, seed_demo_orders
from app.models import Base, Conversation, Execution, Job, Message, Order, Ticket, User
from app.refund_eligibility import evaluate_refund_eligibility, parse_refund_order_id
from app.ticket_lookup import parse_ticket_id
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


def test_development_account_accepts_one_character_password(client):
    """Allow single-character passwords when creating and resetting a development account.
    验证开发账号创建和重置密码时均可使用一个字符的密码。
    """
    save_user("short-password", "1")
    assert client.get("/me", auth=("short-password", "1")).status_code == 200
    save_user("short-password", "2", reset=True)
    assert client.get("/me", auth=("short-password", "1")).status_code == 401
    assert client.get("/me", auth=("short-password", "2")).status_code == 200
    with pytest.raises(ValueError, match="non-empty"):
        save_user("short-password", "", reset=True)


def test_fixed_reply_pipeline_and_pagination(client):
    """Keep ordinary fixed chat on its original path without a business result.
    验证普通固定回复沿用原流程，不生成业务执行记录。
    """
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
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0


@pytest.mark.parametrize(
    "content, order_id, outcome, reasons, amount, reply",
    [
        (
            "  订单 1001 能退款吗？  ",
            1001,
            "ELIGIBLE",
            [],
            12999,
            "Order 1001 is eligible for a simulated refund of CAD 129.99.",
        ),
        (
            "Can order 1002 be refunded?",
            1002,
            "INELIGIBLE",
            ["WINDOW_EXPIRED"],
            None,
            "Order 1002 is not eligible for a simulated refund. The 30-day refund window has expired.",
        ),
        (
            "Can order 1003 be refunded?",
            1003,
            "NEEDS_INFORMATION",
            ["MISSING_DELIVERED_AT"],
            None,
            "More information is required to check order 1003. The delivery date is missing.",
        ),
        ("Can order 1005 be refunded?", 1005, "NOT_FOUND", ["ORDER_NOT_FOUND"], None, "Order not found."),
        ("Can order 9000 be refunded?", 9000, "NOT_FOUND", ["ORDER_NOT_FOUND"], None, "Order not found."),
    ],
)
def test_refund_check_via_message(client, content, order_id, outcome, reasons, amount, reply):
    """Verify the saved result, reply, owner isolation and duplicate receipt through normal routes.
    通过正常接口验证持久化结果、回复、归属隔离和重复提交回执。
    """
    seed_demo_orders("alice", "bob")
    conversation, payload, receipt = submit(client, content=content)
    assert run_once("refund-worker")
    messages_url = f"/conversations/{conversation}/messages"
    assert client.post(messages_url, json=payload).json() == receipt
    assert not run_once("refund-worker")
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"
    messages = client.get(messages_url).json()
    assert [message["content"] for message in messages] == [content, reply]
    assert messages[1]["reply_to_message_id"] == receipt["message_id"]
    with SessionLocal() as session:
        execution = session.scalars(select(Execution)).one()
        assert str(execution.source_message_id) == receipt["message_id"]
        assert execution.requested_order_id == order_id
        assert execution.outcome == outcome
        assert execution.reason_codes == reasons
        assert execution.refund_amount_minor == amount
        assert execution.currency == (None if outcome == "NOT_FOUND" else "CAD")
        assert execution.policy_version == "DEMO-CAD-30D/v1"


@pytest.mark.parametrize(
    "age, outcome, reasons",
    [
        (timedelta(days=30), "ELIGIBLE", ()),
        (timedelta(days=30, seconds=1), "INELIGIBLE", ("WINDOW_EXPIRED",)),
        (timedelta(seconds=-1), "NEEDS_INFORMATION", ("INVALID_DELIVERED_AT",)),
    ],
)
def test_refund_window_uses_utc_instants(age, outcome, reasons):
    """Check the inclusive cutoff and reject a delivery time in the future.
    验证退款截止时刻包含边界，并拒绝将来的交付时间。
    """
    checked_at = datetime(2026, 11, 8, 16, tzinfo=UTC)
    order = Order(
        payment_status="PAID",
        fulfillment_status="DELIVERED",
        paid_total_minor=12999,
        currency="CAD",
        delivered_at=checked_at - age,
    )
    result = evaluate_refund_eligibility(order, checked_at)
    assert (result.outcome, result.reason_codes) == (outcome, reasons)


@pytest.mark.parametrize(
    "content",
    [
        "Can order 0 be refunded?",
        "Can order 9223372036854775808 be refunded?",
        "订单 １００１ 能退款吗？",
        "Quote this: Can order 1001 be refunded?",
    ],
)
def test_refund_request_requires_a_complete_valid_order_number(content):
    """Keep unrelated text and invalid identifiers on the normal chat path.
    将无关文本和无效编号留在普通聊天流程。
    """
    assert parse_refund_order_id(content) is None


@pytest.mark.parametrize(
    "content, order_id, outcome, reply",
    [
        (
            "Look up ticket 391",
            1001,
            "ELIGIBLE",
            "Ticket 391 is open. Order 1001 is eligible for a simulated refund of CAD 129.99.",
        ),
        (
            "查询工单 #392",
            1002,
            "INELIGIBLE",
            "Ticket 392 is open. Order 1002 is not eligible for a simulated refund. "
            "The 30-day refund window has expired.",
        ),
        (
            "Look up ticket 393",
            1003,
            "NEEDS_INFORMATION",
            "Ticket 393 is open. More information is required to check order 1003. The delivery date is missing.",
        ),
        ("Look up ticket 395", None, None, "Ticket not found."),
        ("Look up ticket 900", None, None, "Ticket not found."),
    ],
)
def test_ticket_lookup_via_message(client, content, order_id, outcome, reply):
    """Exercise ticket lookup and its persisted reply through the existing chat API.
    通过现有聊天接口验证工单查询及持久化回复。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, payload, receipt = submit(client, content=content)
    assert run_once("ticket-worker")
    url = f"/conversations/{conversation}/messages"

    # Replaying the request must return the original receipt and leave one reply.
    # 重放请求应返回原有回执，并且只保留一条回复。
    assert client.post(url, json=payload).json() == receipt
    assert not run_once("ticket-worker")
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"
    assert [message["content"] for message in client.get(url).json()] == [content, reply]

    # Only a visible linked order yields a saved eligibility record.
    # 仅在关联订单可见时保存资格记录。
    with SessionLocal() as session:
        records = session.scalars(select(Execution)).all()
        assert [(record.requested_order_id, record.outcome) for record in records] == (
            [(order_id, outcome)] if order_id is not None else []
        )
        assert session.get(Ticket, 391).status == "OPEN"


@pytest.mark.parametrize("linked_order_id", [None, 1005])
def test_ticket_lookup_hides_inaccessible_order(client, accounts, linked_order_id):
    """Treat an inaccessible order like a missing link, without revealing its number.
    将无权访问的订单与未关联订单同等处理，并隐藏其编号。
    """
    seed_demo_orders("alice", "bob")
    with SessionLocal.begin() as session:
        session.add(Ticket(id=398, assigned_user_id=accounts[0], order_id=linked_order_id))
    conversation, _, _ = submit(client, content="Look up ticket 398")
    assert run_once("ticket-worker")
    messages = client.get(f"/conversations/{conversation}/messages").json()
    assert messages[-1]["content"] == (
        "Ticket 398 is open. More information is required. No accessible order is linked to this ticket."
    )
    with SessionLocal() as session:
        assert session.scalars(select(Execution)).first() is None


@pytest.mark.parametrize(
    "content",
    [
        "Look up ticket 0",
        "Look up ticket 9223372036854775808",
        "查询工单 ３９１",
        "Look up ticket 391 and 392",
        "Quote: Look up ticket 391",
    ],
)
def test_ticket_parser_requires_one_complete_valid_number(content):
    """Reject ambiguous, quoted, and out-of-range ticket requests.
    拒绝含歧义、引用或超出范围的工单查询。
    """
    assert parse_ticket_id(content) is None


def test_ticket_seed_preserves_status_and_rejects_relinking(accounts):
    """Allow repeated setup while preserving saved status and order links.
    允许重复初始化，同时保留已保存的状态与订单关联。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    with SessionLocal.begin() as session:
        session.get(Ticket, 391).status = "RESOLVED"
    seed_demo_orders("alice", "bob", with_tickets=True)

    # Simulate a conflicting existing link without changing the assigned account.
    # 在保留原有归属的情况下模拟已有订单关联冲突。
    with SessionLocal.begin() as session:
        ticket = session.get(Ticket, 391)
        assert (ticket.assigned_user_id, ticket.order_id, ticket.status) == (accounts[0], 1001, "RESOLVED")
        ticket.order_id = 1002
    with pytest.raises(ValueError, match="different accounts or order links"):
        seed_demo_orders("alice", "bob", with_tickets=True)
    with SessionLocal() as session:
        assert session.get(Ticket, 391).order_id == 1002


@pytest.mark.parametrize("content", ["Can order 1001 be refunded?", "Look up ticket 391"])
def test_refund_completion_rejects_stale_claim_and_rolls_back(client, content):
    """Keep the execution and reply atomic across worker takeover and a failed update.
    验证 worker 接管或最终更新失败时，资格记录和回复仍保持事务一致。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, _, receipt = submit(client, content=content)
    old = claim_job("old-refund-worker")
    expire(old.job_id)
    new = claim_job("new-refund-worker")
    assert new.job_id == old.job_id and new.token != old.token
    with pytest.raises(LostLease):
        complete_job(old, "Message received.", check_refund_requests=True)

    def fail_final_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        """Inject a failure after the result and reply have been flushed.
        在结果与回复已经写入事务后模拟最终更新失败。
        """
        if statement.startswith("UPDATE jobs"):
            raise SQLAlchemyError("simulated refund completion failure")

    event.listen(engine, "before_cursor_execute", fail_final_update)
    try:
        with pytest.raises(SQLAlchemyError):
            complete_job(new, "Message received.", check_refund_requests=True)
    finally:
        event.remove(engine, "before_cursor_execute", fail_final_update)

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.get(Conversation, conversation).next_message_seq == 2
    assert len(client.get(f"/conversations/{conversation}/messages").json()) == 1
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "RUNNING"

    complete_job(new, "Message received.", check_refund_requests=True)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
    assert len(client.get(f"/conversations/{conversation}/messages").json()) == 2
    assert client.get(f"/jobs/{receipt['job_id']}").json()["status"] == "SUCCEEDED"


def test_demo_seed_preserves_saved_facts_and_rejects_reassignment(accounts):
    """Allow repeated demo setup while protecting existing ownership and order facts.
    允许重复初始化演示数据，同时保护已有订单归属和内容。
    """
    seed_demo_orders("alice", "bob")
    with SessionLocal.begin() as session:
        order = session.get(Order, 1001)
        delivered_at = order.delivered_at
        order.paid_total_minor = 13001
    seed_demo_orders("alice", "bob")
    with pytest.raises(ValueError, match="already belong to different accounts"):
        seed_demo_orders("bob", "alice")
    with SessionLocal() as session:
        order = session.get(Order, 1001)
        assert order.assigned_user_id == accounts[0]
        assert order.paid_total_minor == 13001
        assert order.delivered_at == delivered_at
        assert session.scalar(select(func.count()).select_from(Order)) == 4


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
