"""Exercise approval staleness using committed PostgreSQL facts.
使用已提交的 PostgreSQL 资料验证审批过期与资料变化。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import event, func, select
from test_resolution_workflow import decide, plan
from test_resolution_workflow import demo_tickets as demo_tickets

from app.db import SessionLocal, engine
from app.models import AppliedResolution, Order, Ticket
from app.worker import claim_job, complete_job, run_once


@pytest.mark.parametrize("when", ["before_approval", "after_approval"])
@pytest.mark.parametrize(
    "change,reason",
    [
        ("payment_status", "ORDER_CHANGED"),
        ("fulfillment_status", "ORDER_CHANGED"),
        ("currency", "ORDER_CHANGED"),
        ("delivered_at", "ORDER_CHANGED"),
        ("paid_total_minor", "ORDER_CHANGED"),
        ("order_owner", "OWNERSHIP_CHANGED"),
        ("ticket_owner", "OWNERSHIP_CHANGED"),
        ("ticket_order", "TICKET_ORDER_CHANGED"),
        ("ticket_closed", "TICKET_CLOSED"),
        ("policy", "POLICY_CHANGED"),
    ],
)
def test_changed_authorization_never_applies(
    client, demo_tickets, accounts, monkeypatch, when, change, reason
):
    path, _, _, proposal = plan(client)
    if when == "after_approval":
        assert decide(client, proposal).status_code == 200
    with SessionLocal.begin() as session:
        order = session.get(Order, 1001)
        ticket = session.get(Ticket, 391)
        values = {
            "payment_status": "UNPAID",
            "fulfillment_status": "NOT_DELIVERED",
            "currency": "USD",
            "delivered_at": order.delivered_at - timedelta(days=1),
            "paid_total_minor": 9999,
        }
        if change in values:
            setattr(order, change, values[change])
        elif change == "order_owner":
            order.assigned_user_id = accounts[1]
        elif change == "ticket_owner":
            ticket.assigned_user_id = accounts[1]
        elif change == "ticket_order":
            ticket.order_id = 1002
        elif change == "ticket_closed":
            ticket.status = "RESOLVED"
        else:
            monkeypatch.setattr("app.resolution_workflow.POLICY_VERSION", "new-policy")
    if when == "before_approval":
        assert decide(client, proposal).status_code == 200
    assert run_once()
    rows = client.get(path).json()
    assert rows[0]["proposal"]["status"] == "INVALIDATED"
    assert rows[0]["proposal"]["invalidation_reason"] == reason
    assert rows[0]["proposal"]["revalidated_at"] is not None
    detail = client.get(f"/executions/{rows[0]['execution_id']}").json()
    assert detail["status"] == "INVALIDATED"
    assert detail["invalidation_reason"] == reason
    assert detail["revalidated_at"] == rows[0]["proposal"]["revalidated_at"]
    assert detail["result"] is None
    assert "Applied a simulated refund" not in rows[-1]["content"]
    with SessionLocal() as session:
        assert session.scalar(select(AppliedResolution)) is None
        assert session.get(Ticket, 391).status == ("RESOLVED" if change == "ticket_closed" else "OPEN")


def test_execution_reads_facts_committed_while_waiting_for_lock(client, demo_tickets):
    path, _, _, proposal = plan(client)
    assert decide(client, proposal).status_code == 200
    claim = claim_job("waiting-worker")
    waiting = Event()

    def observe(_conn, _cursor, statement, *_args):
        if statement.startswith("SELECT orders.") and "FOR UPDATE" in statement:
            waiting.set()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with SessionLocal.begin() as session:
                order = session.get(Order, 1001, with_for_update=True)
                order.paid_total_minor = 9999
                future = pool.submit(complete_job, claim)
                assert waiting.wait(3)
                assert not future.done()
            future.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert client.get(path).json()[0]["proposal"]["invalidation_reason"] == "ORDER_CHANGED"
    with SessionLocal() as session:
        assert session.scalar(select(AppliedResolution)) is None
        assert session.get(Ticket, 391).status == "OPEN"


def test_refund_window_expires_without_editing_proposal(client, demo_tickets):
    with SessionLocal.begin() as session:
        session.get(Order, 1001).delivered_at = (
            func.clock_timestamp() - timedelta(days=30) + timedelta(seconds=2)
        )
    path, _, _, proposal = plan(client)
    assert decide(client, proposal).status_code == 200
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            if session.scalar(select(func.clock_timestamp())) >= datetime.fromisoformat(
                proposal["expires_at"]
            ):
                break
        time.sleep(0.05)
    else:
        pytest.fail("Database clock did not reach the natural refund-window boundary")
    assert run_once()
    assert client.get(path).json()[0]["proposal"]["invalidation_reason"] == "PROPOSAL_EXPIRED"
    with SessionLocal() as session:
        assert session.scalar(select(AppliedResolution)) is None
        assert session.get(Ticket, 391).status == "OPEN"


def test_changed_amount_requires_new_identity_and_new_approval(client, demo_tickets):
    path, _, _, old = plan(client)
    receipt = decide(client, old).json()
    with SessionLocal.begin() as session:
        session.get(Order, 1001).paid_total_minor = 9999
    run_once()
    assert client.get(path).json()[0]["proposal"]["status"] == "INVALIDATED"
    new_path, _, _, new = plan(client)
    assert new["id"] != old["id"] and new["refund_amount_minor"] == "9999"
    assert decide(client, old).json() == receipt
    assert not run_once()
    assert decide(client, new).status_code == 200
    assert run_once()
    assert client.get(new_path).json()[0]["proposal"]["status"] == "COMPLETED"
    with SessionLocal() as session:
        assert session.scalars(select(AppliedResolution)).one().refund_amount_minor == 9999
