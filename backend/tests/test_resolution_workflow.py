"""Verify the human decision and simulated refund through the real API and worker.
通过真实 API 和 worker 验证人工审批及模拟退款。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal, engine
from app.event_store import read_event_head, read_events
from app.manage import seed_demo_orders
from app.models import (
    AppliedResolution,
    ConversationEventHead,
    Job,
    Order,
    ProposalDecision,
    ResolutionProposal,
    Ticket,
)
from app.support_workflow import PrepareRefund, prepare_refund
from app.worker import LostLease, claim_job, complete_job, fail_job, run_once


@pytest.fixture
def demo_tickets(accounts):
    """Seed the normal ticket and a second ticket that points to the same order.
    初始化常规工单及另一张指向同一订单的工单。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    with SessionLocal.begin() as session:
        session.add(Ticket(id=396, order_id=1001, assigned_user_id=accounts[0]))


def submit(client, text, conversation=None):
    """Submit through chat, returning the URL, retry payload and initial receipt.
    通过聊天提交，返回会话消息地址、重试请求及初始回执。
    """
    conversation = conversation or client.post("/conversations", json={}).json()["id"]
    path = f"/conversations/{conversation}/messages"
    payload = {"content": text, "submission_key": str(uuid4())}
    response = client.post(path, json=payload)
    assert response.status_code == 202, response.text
    return path, payload, response.json()


def plan(client, ticket_id=391):
    """Process one planning job and read its pending proposal from history.
    处理一个方案准备待办，并从历史读取待审批方案。
    """
    path, payload, receipt = submit(client, f"Prepare a refund for ticket {ticket_id}")
    assert run_once("planning-worker")
    proposal = client.get(path).json()[0]["proposal"]
    assert proposal["status"] == "WAITING_FOR_APPROVAL"
    return path, payload, receipt, proposal


def decide(client, proposal, decision="APPROVE", **kwargs):
    """Post a human decision for the proposal and version shown in chat.
    对聊天中展示的方案及版本提交人工决定。
    """
    return client.post(
        f"/proposals/{proposal['id']}/decision",
        json={"decision": decision, "version": proposal["version"]},
        **kwargs,
    )


def expire_proposal(proposal):
    """Move only this fixture's expiration into the past using database time.
    使用数据库时间，仅将当前测试方案的有效期移至过去。
    """
    with SessionLocal.begin() as session:
        session.get(ResolutionProposal, UUID(proposal["id"])).expires_at = func.clock_timestamp() - timedelta(
            seconds=1
        )


def execution_path(client, history_path):
    return f"/executions/{client.get(history_path).json()[0]['execution_id']}"


def assert_conflict(response, code, detail):
    assert response.status_code == 409
    assert response.json() == {"code": code, "detail": detail}


def test_approved_resolution_preserves_receipts_and_applies_exact_draft(client, demo_tickets):
    """Keep both retry receipts stable and persist exactly the approved business action.
    保持两种重试回执稳定，并按获批内容精确保存业务动作。
    """
    path, payload, initial, proposal = plan(client)
    detail_path = execution_path(client, path)
    pending = client.get(detail_path).json()
    assert pending["source_message_id"] == initial["message_id"]
    assert pending["proposal_id"] == proposal["id"]
    assert pending["expires_at"] == proposal["expires_at"]
    assert pending["status"] == "WAITING_FOR_APPROVAL"
    assert pending["decision"] is pending["resume_job_status"] is pending["result"] is None
    assert pending["revalidated_at"] is pending["invalidation_reason"] is None
    assert decide(client, proposal, auth=("bob", "bob-test-password")).status_code == 404
    assert_conflict(
        decide(client, {**proposal, "version": 2}),
        "PROPOSAL_VERSION_CONFLICT",
        "Proposal version has changed",
    )
    new_message = {"content": "A new turn", "submission_key": str(uuid4())}
    blocked_detail = "Resolve the active refund proposal before sending a new message."
    assert_conflict(client.post(path, json=new_message), "CONVERSATION_BUSY", blocked_detail)
    assert client.post(path, json=payload).json() == initial
    assert_conflict(
        client.post(path, json={**payload, "content": new_message["content"]}),
        "IDEMPOTENCY_CONFLICT",
        "submission_key already used for different content",
    )

    # Approval creates a different job while leaving the original message receipt intact.
    # 批准创建不同的恢复待办，同时保留原有消息回执。
    response = decide(client, proposal)
    assert response.status_code == 200
    decision = response.json()
    assert decision["resume_job_id"] != initial["job_id"]
    submission_retry = client.post(path, json=payload)
    assert submission_retry.status_code == 202 and submission_retry.json() == initial
    assert_conflict(client.post(path, json=new_message), "CONVERSATION_BUSY", blocked_detail)
    assert client.get(path).json()[0]["proposal"]["status"] == "QUEUED"
    queued = client.get(detail_path).json()
    assert queued["status"] == queued["resume_job_status"] == "QUEUED"
    assert queued["decision"] == decision
    assert queued["result"] is None
    claim = claim_job("apply-worker")
    assert claim.kind == "RESUME_EXECUTION"
    assert_conflict(client.post(path, json=new_message), "CONVERSATION_BUSY", blocked_detail)
    assert client.get(path).json()[0]["proposal"]["status"] == "RUNNING"
    running = client.get(detail_path).json()
    assert running["status"] == running["resume_job_status"] == "RUNNING"
    assert running["result"] is None
    complete_job(claim)

    messages = client.get(path).json()
    assert messages[0]["proposal"]["status"] == "COMPLETED"
    assert messages[0]["proposal"]["decision"] == "APPROVE"
    assert messages[1]["reply_to_message_id"] == initial["message_id"]
    assert "CAD 129.99" in messages[1]["content"]
    decision_retry = decide(client, proposal)
    assert decision_retry.status_code == 200 and decision_retry.json() == decision
    assert_conflict(
        decide(client, proposal, "REJECT"),
        "DECISION_CONFLICT",
        "A different decision has already been recorded",
    )
    detail = client.get(detail_path).json()
    assert detail["status"] == "COMPLETED"
    assert detail["resume_job_status"] == "SUCCEEDED"
    assert detail["decision"] == decision
    assert detail["revalidated_at"] == messages[0]["proposal"]["revalidated_at"]
    assert detail["revalidated_at"] is not None
    assert detail["invalidation_reason"] is None
    result = detail["result"]
    applied_at = datetime.fromisoformat(result.pop("applied_at"))
    assert result == {
        "order_id": "1001",
        "ticket_id": "391",
        "refund_amount_minor": "12999",
        "currency": "CAD",
        "customer_reply": proposal["customer_reply"],
    }
    with SessionLocal() as session:
        action = session.scalars(select(AppliedResolution)).one()
        assert (action.order_id, action.ticket_id, action.refund_amount_minor) == (1001, 391, 12999)
        assert action.customer_reply == proposal["customer_reply"]
        assert action.applied_at == applied_at
        assert session.get(Ticket, 391).status == "RESOLVED"
        # A successful refund publishes its decision and final results once, in causal order.
        # 成功退款按因果顺序记录一次审批决定和最终结果。
        events = read_events(session, UUID(messages[0]["conversation_id"]))
        assert [(row.seq, row.event_type) for row in events] == list(
            enumerate(
                [
                    "message.created",
                    "job.status_changed",
                    "job.status_changed",
                    "proposal.created",
                    "job.status_changed",
                    "proposal.decided",
                    "job.status_changed",
                    "job.status_changed",
                    "resolution.completed",
                    "message.created",
                    "job.status_changed",
                ],
                start=1,
            )
        )
        assert events[3].payload["proposal_id"] == proposal["id"]
        assert events[5].payload["decision"] == "APPROVE"
        assert events[5].payload["resume_job_id"] == decision["resume_job_id"]
        assert events[8].payload["proposal_id"] == proposal["id"]
        assert events[9].payload["message_id"] == messages[1]["id"]
        assert events[10].payload["status"] == "SUCCEEDED"

    other, _, _ = submit(client, "Can order 1001 be refunded?")
    assert run_once("lookup-worker")
    assert "already been applied" in client.get(other).json()[-1]["content"]


def test_rejection_leaves_business_data_unchanged(client, demo_tickets):
    """Reject without creating a continuation or mutating the ticket and order.
    拒绝时不创建恢复待办，并保留工单及订单原状。
    """
    path, _, _, proposal = plan(client)
    response = decide(client, proposal, "REJECT")
    assert response.status_code == 200
    assert response.json()["resume_job_id"] is None
    assert decide(client, proposal, "REJECT").json() == response.json()
    assert client.get(path).json()[0]["proposal"]["status"] == "REJECTED"
    detail = client.get(execution_path(client, path)).json()
    assert detail["status"] == "REJECTED"
    assert detail["decision"] == response.json()
    assert detail["resume_job_status"] is detail["result"] is None
    with SessionLocal() as session:
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.get(Order, 1001).paid_total_minor == 12999
        assert session.scalars(select(AppliedResolution)).first() is None
        assert session.scalars(select(Job).where(Job.kind == "RESUME_EXECUTION")).first() is None
    assert client.post(path, json={"content": "Hello", "submission_key": str(uuid4())}).status_code == 202


def test_execution_detail_retains_approval_when_continuation_fails(client, demo_tickets):
    path, _, _, proposal = plan(client)
    response = decide(client, proposal)
    assert response.status_code == 200
    claim = claim_job("failing-worker")
    assert str(claim.job_id) == response.json()["resume_job_id"]
    fail_job(claim, RuntimeError("Failure before committing the resolution"))

    detail = client.get(execution_path(client, path)).json()
    assert detail["status"] == detail["resume_job_status"] == "FAILED"
    assert detail["decision"] == response.json()
    assert detail["result"] is None
    assert "last_error" not in detail


def test_execution_detail_serializes_bigint_numbers_and_amounts_as_strings(client, accounts):
    order_id = 2**63 - 1
    ticket_id = order_id - 1
    amount = order_id - 2
    with SessionLocal.begin() as session:
        session.add(
            Order(
                id=order_id,
                assigned_user_id=accounts[0],
                payment_status="PAID",
                fulfillment_status="DELIVERED",
                paid_total_minor=amount,
                currency="CAD",
                delivered_at=func.clock_timestamp() - timedelta(days=1),
            )
        )
        session.flush()
        session.add(Ticket(id=ticket_id, order_id=order_id, assigned_user_id=accounts[0]))

    path, _, _, proposal = plan(client, ticket_id=ticket_id)
    assert decide(client, proposal).status_code == 200
    assert run_once("apply-worker")
    detail = client.get(execution_path(client, path)).json()
    assert detail["order_id"] == detail["result"]["order_id"] == str(order_id)
    assert detail["result"]["ticket_id"] == str(ticket_id)
    assert detail["refund_amount_minor"] == detail["result"]["refund_amount_minor"] == str(amount)


@pytest.mark.parametrize("second_decision", ["APPROVE", "REJECT"])
def test_concurrent_decisions_create_one_receipt(client, demo_tickets, second_decision):
    """Serialize identical retries and opposing decisions under the conversation lock.
    在会话锁保护下串行处理相同重试及相反决定。
    """
    path, _, _, proposal = plan(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(decide, client, proposal, "APPROVE")
        second = pool.submit(decide, client, proposal, second_decision)
        responses = [first.result(), second.result()]
    if second_decision == "APPROVE":
        assert [response.status_code for response in responses] == [200, 200]
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(response.status_code for response in responses) == [200, 409]
    with SessionLocal() as session:
        saved = session.scalars(select(ProposalDecision)).one()
        resumes = session.scalars(select(Job).where(Job.kind == "RESUME_EXECUTION")).all()
        assert len(resumes) == int(saved.decision == "APPROVE")
        conversation_id = UUID(path.split("/")[2])
        decisions = [
            row.payload
            for row in read_events(session, conversation_id)
            if row.event_type == "proposal.decided"
        ]
        assert len(decisions) == 1
        assert decisions[0]["decision"] == saved.decision
        assert decisions[0]["resume_job_id"] == (str(saved.resume_job_id) if saved.resume_job_id else None)


@pytest.mark.parametrize("expired", [False, True], ids=["approval", "expiration"])
def test_decision_rolls_back_if_event_batch_cannot_be_saved(client, demo_tickets, expired):
    """Roll back approval or expiration when its event batch cannot commit.
    事件批次无法提交时回滚批准或过期处理。
    """
    path, _, _, proposal = plan(client)
    conversation_id = UUID(path.split("/")[2])
    if expired:
        expire_proposal(proposal)
    with SessionLocal() as session:
        before = read_event_head(session, conversation_id)

    def fail_event_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        """Fail at the event insert after the business writes are flushed.
        业务变更写入事务后，在插入事件时注入失败。
        """
        if statement.startswith("INSERT INTO conversation_events"):
            raise SQLAlchemyError("simulated event write failure")

    event.listen(engine, "before_cursor_execute", fail_event_insert)
    try:
        failed = decide(client, proposal)
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Database temporarily unavailable"}
    finally:
        event.remove(engine, "before_cursor_execute", fail_event_insert)
    with SessionLocal() as session:
        assert read_event_head(session, conversation_id) == before
        assert read_events(session, conversation_id, after_seq=before) == []
        assert session.scalars(select(ProposalDecision)).first() is None
        assert session.scalars(select(Job).where(Job.kind == "RESUME_EXECUTION")).first() is None
    (source,) = client.get(path).json()
    assert source["proposal"]["status"] == "WAITING_FOR_APPROVAL"
    retry = decide(client, proposal)
    with SessionLocal() as session:
        first, second = read_events(session, conversation_id, after_seq=before)
    assert (first.seq, second.seq) == (before + 1, before + 2)
    if expired:
        assert_conflict(retry, "PROPOSAL_EXPIRED", "Proposal has expired")
        assert (first.event_type, second.event_type) == ("resolution.invalidated", "message.created")
        assert first.payload["reason"] == "EXPIRED"
    else:
        assert retry.status_code == 200
        assert (first.event_type, second.event_type) == ("proposal.decided", "job.status_changed")
        assert second.payload["status"] == "QUEUED"


@pytest.mark.parametrize(
    "change", ["expired_before_approval", "expired_by_worker", "expired_after_approval", "changed_order"]
)
def test_expired_or_changed_proposal_never_writes_business_data(client, demo_tickets, change):
    """Invalidate stale input and retain any approval receipt that already exists.
    使过时方案失效，并保留此前已存在的批准回执。
    """
    path, _, initial, proposal = plan(client)
    conversation_id = UUID(path.split("/")[2])
    decision = resume_job_status = None
    if change in {"expired_before_approval", "expired_by_worker"}:
        expire_proposal(proposal)
        if change == "expired_by_worker":
            assert run_once("expiry-worker")
        else:
            assert_conflict(decide(client, proposal), "PROPOSAL_EXPIRED", "Proposal has expired")
        assert_conflict(
            decide(client, proposal), "PROPOSAL_EXPIRED", "Proposal is no longer waiting for approval"
        )
    else:
        response = decide(client, proposal)
        assert response.status_code == 200
        decision = response.json()
        resume_job_status = "SUCCEEDED"
        if change == "expired_after_approval":
            expire_proposal(proposal)
        else:
            with SessionLocal.begin() as session:
                session.get(Order, 1001).paid_total_minor = 13000
        assert run_once("apply-worker")
        retry = decide(client, proposal)
        assert retry.status_code == 200 and retry.json() == decision
    source, reply = client.get(path).json()
    assert reply["reply_to_message_id"] == initial["message_id"]
    assert source["proposal"]["status"] == "INVALIDATED"
    detail_path = f"/executions/{source['execution_id']}"
    response = client.get(detail_path)
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "INVALIDATED"
    assert detail["outcome"] == "ELIGIBLE"
    expected_reason = "ORDER_CHANGED" if change == "changed_order" else "PROPOSAL_EXPIRED"
    assert detail["invalidation_reason"] == source["proposal"]["invalidation_reason"] == expected_reason
    assert detail["revalidated_at"] == source["proposal"]["revalidated_at"]
    assert detail["revalidated_at"] is not None
    assert detail["decision"] == decision
    assert detail["resume_job_status"] == resume_job_status
    assert detail["result"] is None
    forbidden = client.get(detail_path, auth=("bob", "bob-test-password"))
    assert forbidden.status_code == 404
    assert forbidden.json() == {"detail": "Execution not found"}
    with SessionLocal() as session:
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.scalars(select(AppliedResolution)).first() is None
        if decision is None:
            assert session.get(ProposalDecision, UUID(proposal["id"])) is None
            assert session.scalar(select(Job.id).where(Job.kind == "RESUME_EXECUTION")) is None
        rows = read_events(session, conversation_id)
        outcomes = [row for row in rows if row.event_type.startswith("resolution.")]
        assert len(outcomes) == 1
        assert outcomes[0].event_type == "resolution.invalidated"
        assert outcomes[0].payload["reason"] == (
            "BUSINESS_STATE_CHANGED" if change == "changed_order" else "EXPIRED"
        )
        assert any(
            row.event_type == "message.created" and row.payload["message_id"] == reply["id"] for row in rows
        )


def test_legacy_invalidated_proposal_without_event_uses_not_pending_conflict(client, demo_tickets):
    path, _, _, proposal = plan(client)
    expire_proposal(proposal)
    assert run_once("expiry-worker")

    # Simulate a proposal closed before the durable event migration.
    # 模拟持久化事件迁移前已关闭的方案。
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "0006_approved_resolutions")
    command.upgrade(cfg, "head")
    source, _ = client.get(path).json()
    detail = client.get(f"/executions/{source['execution_id']}")
    assert detail.status_code == 200
    saved = detail.json()
    assert saved["status"] == "INVALIDATED"
    assert saved["revalidated_at"] is saved["invalidation_reason"] is None
    assert_conflict(
        decide(client, proposal), "PROPOSAL_NOT_PENDING", "Proposal is no longer waiting for approval"
    )


@pytest.mark.parametrize("release", ["approve", "expire"])
def test_later_queued_turn_waits_for_earlier_proposal(client, demo_tickets, release):
    """Keep queued turns behind their pending predecessor and unblock them at completion.
    让排队轮次等待前面的待审批方案，在结束后解除阻塞。
    """
    path, _, _ = submit(client, "Prepare a refund for ticket 391")
    response = client.post(path, json={"content": "Later turn", "submission_key": str(uuid4())})
    assert response.status_code == 202
    assert run_once("planning-worker")
    proposal = client.get(path).json()[0]["proposal"]
    assert not run_once("other-worker")
    if release == "approve":
        assert decide(client, proposal).status_code == 200
    else:
        expire_proposal(proposal)
    assert run_once("apply-or-expire-worker")
    assert client.get(f"/jobs/{response.json()['job_id']}").json()["status"] == "QUEUED"
    assert run_once("later-worker")
    messages = client.get(path).json()
    assert [row["role"] for row in messages] == ["user", "user", "assistant", "assistant"]
    assert [row["reply_to_message_id"] for row in messages[2:]] == [row["id"] for row in messages[:2]]


@pytest.mark.parametrize("later_action", ["reject", "expire"])
def test_upgraded_pending_proposals_finalize_in_source_order(client, demo_tickets, accounts, later_action):
    """Upgrade planning-only history with two pending proposals and preserve turn order.
    升级包含两份待审批方案的历史记录，并保持来源消息顺序。
    """
    path, _, first = submit(client, "Prepare a refund for ticket 391")
    second = client.post(
        path, json={"content": "Prepare a refund for ticket 396", "submission_key": str(uuid4())}
    ).json()
    # Reproduce the planning-only release, where both source jobs could complete before any decision.
    # 复现仅支持方案准备的版本，两个来源待办都可在审批前完成。
    with SessionLocal.begin() as session:
        for receipt, ticket_id in [(first, 391), (second, 396)]:
            proposal = prepare_refund(
                session, UUID(receipt["message_id"]), accounts[0], PrepareRefund(ticket_id=ticket_id)
            )
            assert isinstance(proposal, ResolutionProposal)
            job = session.get(Job, UUID(receipt["job_id"]))
            job.status = "SUCCEEDED"
            job.attempts = 1
            job.started_at = job.finished_at = func.clock_timestamp()
    original = [row["proposal"]["id"] for row in client.get(path).json()]
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "0005_resolution_proposals")
    command.upgrade(cfg, "head")
    earlier, later = [row["proposal"] for row in client.get(path).json()]
    assert [earlier["id"], later["id"]] == original

    # Rejecting or expiring the later proposal must wait for the older continuation.
    # 拒绝或终止已过期的后续方案，都必须等待前序恢复待办。
    busy_detail = "Resolve earlier turns in this conversation before deciding this proposal"
    assert_conflict(decide(client, later, "REJECT"), "CONVERSATION_BUSY", busy_detail)
    if later_action == "expire":
        expire_proposal(later)
        assert not run_once("expiry-worker")
    assert decide(client, earlier).status_code == 200
    assert_conflict(decide(client, later, "REJECT"), "CONVERSATION_BUSY", busy_detail)
    assert run_once("continuation-worker")
    if later_action == "expire":
        assert run_once("expiry-worker")
    else:
        assert decide(client, later, "REJECT").status_code == 200
    rows = client.get(path).json()
    assert [row["reply_to_message_id"] for row in rows[2:]] == [first["message_id"], second["message_id"]]
    assert rows[0]["proposal"]["status"] == "COMPLETED"
    assert rows[1]["proposal"]["status"] == ("INVALIDATED" if later_action == "expire" else "REJECTED")


def test_parallel_proposals_can_refund_order_only_once(client, demo_tickets):
    """Serialize two approved tickets targeting one order, without partially applying the loser.
    串行执行指向同一订单的两个已批准工单，竞争失败的方案不留下部分业务变更。
    """
    paths_and_proposals = [plan(client, ticket_id) for ticket_id in (391, 396)]
    for _, _, _, proposal in paths_and_proposals:
        assert decide(client, proposal).status_code == 200
    claims = [claim_job("apply-a"), claim_job("apply-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(complete_job, claims))
    states = [client.get(path).json()[0]["proposal"]["status"] for path, *_ in paths_and_proposals]
    assert sorted(states) == ["COMPLETED", "INVALIDATED"]
    for (path, _, _, proposal), state in zip(paths_and_proposals, states, strict=True):
        detail = client.get(execution_path(client, path)).json()
        assert detail["status"] == state
        assert detail["decision"]["proposal_id"] == proposal["id"]
        if state == "COMPLETED":
            assert detail["result"]["ticket_id"] == proposal["ticket_id"]
        else:
            assert detail["result"] is None
    with SessionLocal() as session:
        action = session.scalars(select(AppliedResolution)).one()
        assert session.get(Ticket, action.ticket_id).status == "RESOLVED"
        other_ticket = 396 if action.ticket_id == 391 else 391
        assert session.get(Ticket, other_ticket).status == "OPEN"


def test_worker_takeover_rolls_back_partial_resolution(client, demo_tickets):
    """Reject an old lease and roll back all business changes if job completion fails.
    拒绝过期租约，并在待办完成失败时回滚全部业务变更。
    """
    path, _, _, proposal = plan(client)
    decision = decide(client, proposal)
    assert decision.status_code == 200
    conversation_id = UUID(path.split("/")[2])
    old = claim_job("old-worker")
    with SessionLocal.begin() as session:
        session.get(Job, old.job_id).lease_expires_at = func.clock_timestamp() - timedelta(seconds=1)
    new = claim_job("new-worker")
    assert new.job_id == old.job_id and new.token != old.token
    with pytest.raises(LostLease):
        complete_job(old)
    fail_job(old, RuntimeError("stale worker"))
    with SessionLocal() as session:
        before = read_event_head(session, conversation_id)
        states = [
            (row.payload["status"], row.payload["attempts"])
            for row in read_events(session, conversation_id)
            if row.payload.get("job_id") == decision.json()["resume_job_id"]
        ]
        assert states == [("QUEUED", 0), ("RUNNING", 1), ("RUNNING", 2)]

    def fail_job_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        """Fail after business writes and events flush but before the fenced completion.
        在业务变更及事件写入事务后、租约保护的完成更新前注入错误。
        """
        if statement.startswith("UPDATE jobs"):
            raise SQLAlchemyError("simulated completion failure")

    event.listen(engine, "before_cursor_execute", fail_job_update)
    try:
        with pytest.raises(SQLAlchemyError):
            complete_job(new)
    finally:
        event.remove(engine, "before_cursor_execute", fail_job_update)
    with SessionLocal() as session:
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.scalars(select(AppliedResolution)).first() is None
        assert read_event_head(session, conversation_id) == before
        assert read_events(session, conversation_id, after_seq=before) == []
    assert len(client.get(path).json()) == 1
    complete_job(new)
    with pytest.raises(LostLease):
        complete_job(new)
    assert len(client.get(path).json()) == 2
    with SessionLocal() as session:
        assert session.scalars(select(AppliedResolution)).one().ticket_id == 391
        outcome, reply, job = read_events(session, conversation_id, after_seq=before)
        assert outcome.seq == before + 1 and outcome.event_type == "resolution.completed"
        assert reply.event_type == "message.created"
        assert job.payload["status"] == "SUCCEEDED" and job.payload["attempts"] == 2


def test_lease_expires_while_waiting_for_event_counter(client, demo_tickets):
    """Rollback staged work if ownership expires while the worker waits for its event counter.
    worker 等待事件计数器的期间若租约到期，回滚已暂存的处置。
    """
    path, _, _, proposal = plan(client)
    conversation_id = UUID(path.split("/")[2])
    assert decide(client, proposal).status_code == 200
    current = claim_job("short-lease-worker", lease_seconds=3)
    lease_id = current.job_id
    attempting_append = Event()

    def note_counter_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        """Signal that completion has passed its initial lease check and reached the counter.
        通知测试，完成流程已经通过首次租约校验并到达计数器更新。
        """
        if statement.startswith("UPDATE conversation_event_heads"):
            attempting_append.set()

    event.listen(engine, "before_cursor_execute", note_counter_update)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with SessionLocal.begin() as holder:
                before = holder.get(
                    ConversationEventHead, conversation_id, with_for_update=True
                ).last_event_seq
                completion = pool.submit(complete_job, current)
                assert attempting_append.wait(timeout=5)
                # Leave the counter locked until the database clock crosses the saved expiration.
                # 保持计数器加锁，直到数据库时钟跨过已保存的租约截止时间。
                remaining = holder.scalar(
                    select(func.extract("epoch", Job.lease_expires_at - func.clock_timestamp())).where(
                        Job.id == lease_id
                    )
                )
                holder.execute(select(func.pg_sleep(max(0, float(remaining)) + 0.05)))
            with pytest.raises(LostLease):
                completion.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", note_counter_update)

    with SessionLocal() as session:
        assert read_event_head(session, conversation_id) == before
        assert session.get(Job, lease_id).status == "RUNNING"
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.scalars(select(AppliedResolution)).first() is None
    assert len(client.get(path).json()) == 1
    assert claim_job("replacement-worker").job_id == lease_id
