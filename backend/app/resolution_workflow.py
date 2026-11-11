"""Manage approved resolutions inside the same database as worker and chat state.
在与待办和聊天状态共用的数据库中管理审批处置。
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    AppliedResolution,
    Conversation,
    Execution,
    Job,
    Message,
    Order,
    ProposalDecision,
    ResolutionProposal,
    Ticket,
)
from app.refund_eligibility import POLICY_VERSION, evaluate_refund_eligibility
from app.support_workflow import order_facts

PROPOSAL_VERSION = 1
EXPIRED_REPLY = "The refund proposal expired. Request a new proposal to continue."


def append_final_reply(session: Session, conversation: Conversation, source_id: UUID, reply: str):
    """Stage the final reply and allocate its sequence under the conversation lock.
    在持有会话锁时暂存最终回复并分配顺序号。
    """
    session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content=reply,
            seq=conversation.next_message_seq,
            reply_to_message_id=source_id,
        )
    )
    conversation.next_message_seq += 1
    conversation.updated_at = func.clock_timestamp()


def close_proposal(
    session: Session, conversation: Conversation, execution: Execution, status: str, reply: str
):
    """Save a terminal proposal state and its final chat reply together.
    将方案终态和最终聊天回复一起保存。
    """
    execution.status = status
    append_final_reply(session, conversation, execution.source_message_id, reply)


def active_resolution_exists(session: Session, conversation_id: UUID) -> bool:
    """Check whether a new turn must wait for an existing proposal or continuation.
    检查新消息是否需要等待已有方案或恢复待办。
    """
    return session.scalar(
        select(
            select(Execution.id)
            .join(Message, Message.id == Execution.source_message_id)
            .join(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
            .outerjoin(ProposalDecision, ProposalDecision.proposal_id == ResolutionProposal.id)
            .outerjoin(Job, Job.id == ProposalDecision.resume_job_id)
            .where(
                Message.conversation_id == conversation_id,
                or_(
                    (Execution.status == "WAITING_FOR_APPROVAL")
                    & (ResolutionProposal.expires_at > func.clock_timestamp()),
                    (Execution.status == "APPROVED") & Job.status.in_(["QUEUED", "RUNNING"]),
                ),
            )
            .exists()
        )
    )


def unfinished_turn_before(conversation_id, source_seq):
    """Build a predicate for any earlier unfinished turn in the same conversation.
    构造谓词，用于检查同一会话中是否仍有未完成的较早轮次。
    """
    earlier = aliased(Message)
    # A pending proposal still owns its turn after the initial job has completed.
    # 初始待办结束后，待审批方案仍占用原会话轮次。
    queued_job = (
        select(Job.id)
        .where(Job.source_message_id == earlier.id, Job.status.in_(["QUEUED", "RUNNING"]))
        .correlate(earlier)
        .exists()
    )
    waiting_proposal = (
        select(Execution.id)
        .where(Execution.source_message_id == earlier.id, Execution.status == "WAITING_FOR_APPROVAL")
        .correlate(earlier)
        .exists()
    )
    return (
        select(earlier.id)
        .where(
            earlier.conversation_id == conversation_id,
            earlier.seq < source_seq,
            or_(queued_job, waiting_proposal),
        )
        .correlate_except(earlier)
        .exists()
    )


def expire_pending_proposal(session: Session) -> bool:
    """Close one expired waiting proposal without blocking another worker's conversation.
    关闭一份已过期待审批方案，并跳过其他 worker 锁定的会话。
    """
    # Lock the conversation first, as the decision API does, and verify the state again after locking.
    # 与审批接口一样先锁定会话，取得锁后再次核对方案状态。
    row = session.execute(
        select(Conversation, Execution.id, Message.seq)
        .join(Message, Message.conversation_id == Conversation.id)
        .join(Execution, Execution.source_message_id == Message.id)
        .join(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .where(
            Execution.status == "WAITING_FOR_APPROVAL",
            ResolutionProposal.expires_at <= func.clock_timestamp(),
            ~unfinished_turn_before(Conversation.id, Message.seq),
        )
        .order_by(ResolutionProposal.expires_at, ResolutionProposal.id)
        .with_for_update(of=Conversation, skip_locked=True)
        .limit(1)
    ).one_or_none()
    if row is None:
        return False
    conversation, execution_id, source_seq = row
    execution = session.get(Execution, execution_id, with_for_update=True)
    if execution.status != "WAITING_FOR_APPROVAL":
        return False
    # Read the predecessor state again under the conversation lock before writing the final reply.
    # 在写入最终回复前，持有会话锁再次读取前序轮次状态。
    if session.scalar(select(unfinished_turn_before(conversation.id, source_seq))):
        return False
    close_proposal(session, conversation, execution, "INVALIDATED", EXPIRED_REPLY)
    return True


def apply_approved_resolution(session: Session, job: Job, conversation: Conversation):
    """Stage the approved simulated action and final reply inside the worker's fenced transaction.
    在 worker 的租约保护事务中暂存已批准的模拟动作和最终回复。
    """
    execution, proposal, decision = session.execute(
        select(Execution, ResolutionProposal, ProposalDecision)
        .join(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .join(ProposalDecision, ProposalDecision.proposal_id == ResolutionProposal.id)
        .where(
            Execution.source_message_id == job.source_message_id,
            Execution.status == "APPROVED",
            ProposalDecision.decision == "APPROVE",
            ProposalDecision.resume_job_id == job.id,
        )
        .with_for_update(of=Execution)
    ).one()

    # Lock the order before the ticket so concurrent proposals for one order serialize.
    # 先锁订单再锁工单，使同一订单的多个方案串行核验。
    order = session.get(Order, execution.requested_order_id, with_for_update=True)
    ticket = session.get(Ticket, proposal.ticket_id, with_for_update=True)
    now = session.scalar(select(func.clock_timestamp()))
    existing_refund = session.scalar(
        select(AppliedResolution.proposal_id).where(
            AppliedResolution.order_id == execution.requested_order_id
        )
    )
    if now >= proposal.expires_at:
        close_proposal(session, conversation, execution, "INVALIDATED", EXPIRED_REPLY)
        return
    if (
        order is None
        or ticket is None
        or decision.user_id != conversation.user_id
        or order.assigned_user_id != conversation.user_id
        or ticket.assigned_user_id != conversation.user_id
        or ticket.order_id != order.id
        or ticket.status != "OPEN"
        or existing_refund is not None
        or execution.policy_version != POLICY_VERSION
        or order_facts(order) != proposal.expected_order_facts
    ):
        close_proposal(
            session,
            conversation,
            execution,
            "INVALIDATED",
            "The refund proposal no longer matches the ticket or order. Request a new proposal.",
        )
        return

    # Recalculate the amount at application time using the current policy and locked facts.
    # 使用当前退款规则和已锁定的订单资料重新计算执行金额。
    result = evaluate_refund_eligibility(order, now)
    if result.outcome != "ELIGIBLE" or (result.refund_amount_minor, result.currency) != (
        execution.refund_amount_minor,
        execution.currency,
    ):
        close_proposal(
            session,
            conversation,
            execution,
            "INVALIDATED",
            "The order no longer qualifies for the approved refund. Request a new proposal.",
        )
        return

    # The refund, exact customer reply, ticket update, and job completion commit as one unit.
    # 退款、原样客户回复、工单变更与待办完成作为一个事务提交。
    session.add(
        AppliedResolution(
            proposal_id=proposal.id,
            order_id=order.id,
            ticket_id=ticket.id,
            refund_amount_minor=result.refund_amount_minor,
            currency=result.currency,
            customer_reply=proposal.customer_reply,
            applied_at=now,
        )
    )
    ticket.status = proposal.proposed_ticket_status
    dollars, cents = divmod(result.refund_amount_minor, 100)
    close_proposal(
        session,
        conversation,
        execution,
        "COMPLETED",
        f"Applied a simulated refund of {result.currency} {dollars}.{cents:02d} to order {order.id}, "
        f"recorded the customer reply, and resolved ticket {ticket.id}.",
    )
