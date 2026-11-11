"""Verify the human decision and simulated refund through the real API and worker.
通过真实 API 和 worker 验证人工审批及模拟退款。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal, engine
from app.manage import seed_demo_orders
from app.models import AppliedResolution, Job, Order, ProposalDecision, ResolutionProposal, Ticket
from app.worker import LostLease, claim_job, complete_job, run_once


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


def test_approved_resolution_preserves_receipts_and_applies_exact_draft(client, demo_tickets):
    """Keep both retry receipts stable and persist exactly the approved business action.
    保持两种重试回执稳定，并按获批内容精确保存业务动作。
    """
    path, payload, initial, proposal = plan(client)
    assert decide(client, proposal, auth=("bob", "bob-test-password")).status_code == 404
    assert decide(client, {**proposal, "version": 2}).status_code == 409
    blocked = client.post(path, json={"content": "Approve it", "submission_key": str(uuid4())})
    assert blocked.status_code == 409

    # Approval creates a different job while leaving the original message receipt intact.
    # 批准创建不同的恢复待办，同时保留原有消息回执。
    response = decide(client, proposal)
    assert response.status_code == 200
    decision = response.json()
    assert decision["resume_job_id"] != initial["job_id"]
    assert client.post(path, json=payload).json() == initial
    assert client.get(path).json()[0]["proposal"]["status"] == "QUEUED"
    claim = claim_job("apply-worker")
    assert claim.kind == "RESUME_EXECUTION"
    assert client.get(path).json()[0]["proposal"]["status"] == "RUNNING"
    complete_job(claim)

    messages = client.get(path).json()
    assert messages[0]["proposal"]["status"] == "COMPLETED"
    assert messages[0]["proposal"]["decision"] == "APPROVE"
    assert messages[1]["reply_to_message_id"] == initial["message_id"]
    assert "CAD 129.99" in messages[1]["content"]
    assert decide(client, proposal).json() == decision
    assert decide(client, proposal, "REJECT").status_code == 409
    with SessionLocal() as session:
        action = session.scalars(select(AppliedResolution)).one()
        assert (action.order_id, action.ticket_id, action.refund_amount_minor) == (1001, 391, 12999)
        assert action.customer_reply == proposal["customer_reply"]
        assert session.get(Ticket, 391).status == "RESOLVED"

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
    with SessionLocal() as session:
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.get(Order, 1001).paid_total_minor == 12999
        assert session.scalars(select(AppliedResolution)).first() is None
        assert session.scalars(select(Job).where(Job.kind == "RESUME_EXECUTION")).first() is None
    assert client.post(path, json={"content": "Hello", "submission_key": str(uuid4())}).status_code == 202


@pytest.mark.parametrize("second_decision", ["APPROVE", "REJECT"])
def test_concurrent_decisions_create_one_receipt(client, demo_tickets, second_decision):
    """Serialize identical retries and opposing decisions under the conversation lock.
    在会话锁保护下串行处理相同重试及相反决定。
    """
    _, _, _, proposal = plan(client)
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


@pytest.mark.parametrize("change", ["expired_before_approval", "expired_after_approval", "changed_order"])
def test_expired_or_changed_proposal_never_writes_business_data(client, demo_tickets, change):
    """Invalidate stale input and retain any approval receipt that already exists.
    使过时方案失效，并保留此前已存在的批准回执。
    """
    path, _, _, proposal = plan(client)
    if change == "expired_before_approval":
        expire_proposal(proposal)
        assert decide(client, proposal).status_code == 409
    else:
        response = decide(client, proposal)
        assert response.status_code == 200
        if change == "expired_after_approval":
            expire_proposal(proposal)
        else:
            with SessionLocal.begin() as session:
                session.get(Order, 1001).paid_total_minor = 13000
        assert run_once("apply-worker")
        assert decide(client, proposal).json() == response.json()
    assert client.get(path).json()[0]["proposal"]["status"] == "INVALIDATED"
    with SessionLocal() as session:
        assert session.get(Ticket, 391).status == "OPEN"
        assert session.scalars(select(AppliedResolution)).first() is None


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
    assert decide(client, proposal).status_code == 200
    old = claim_job("old-worker")
    with SessionLocal.begin() as session:
        session.get(Job, old.job_id).lease_expires_at = func.clock_timestamp() - timedelta(seconds=1)
    new = claim_job("new-worker")
    assert new.job_id == old.job_id and new.token != old.token
    with pytest.raises(LostLease):
        complete_job(old)

    def fail_job_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        """Fail after business writes have flushed but before the fenced completion.
        在业务变更写入事务后、租约保护的完成更新前注入错误。
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
    assert len(client.get(path).json()) == 1
    complete_job(new)
    with pytest.raises(LostLease):
        complete_job(new)
    assert len(client.get(path).json()) == 2
    with SessionLocal() as session:
        assert session.scalars(select(AppliedResolution)).one().ticket_id == 391
