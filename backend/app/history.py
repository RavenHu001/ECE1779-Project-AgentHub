# Read public history and saved execution results.
# 读取公开的历史消息和已保存的执行结果。

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import read_activity
from app.event_store import read_event_head
from app.models import (
    AppliedResolution,
    Conversation,
    Execution,
    Job,
    Message,
    ProposalDecision,
    ResolutionProposal,
)
from app.resolution_workflow import PROPOSAL_VERSION
from app.schemas import (
    ActivityOut,
    AppliedResolutionOut,
    ExecutionDetailOut,
    MessageOut,
    ProposalDecisionOut,
    ProposalOut,
)


def _history_query(conversation_id: UUID):
    """Join each message to its optional proposal, decision and continuation job.
    将每条消息与其可选的方案、审批决定及恢复待办关联，且每条消息只返回一行。
    """
    return (
        select(Message, Execution, ResolutionProposal, ProposalDecision, Job)
        .outerjoin(Execution, Execution.source_message_id == Message.id)
        .outerjoin(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .outerjoin(ProposalDecision, ProposalDecision.proposal_id == ResolutionProposal.id)
        .outerjoin(Job, Job.id == ProposalDecision.resume_job_id)
        .where(Message.conversation_id == conversation_id)
    )


def _execution_status(execution: Execution, job: Job | None) -> str:
    """Use the continuation's progress while the execution is approved.
    执行记录处于已批准状态时，使用恢复待办的进度。
    """
    if execution.status == "APPROVED":
        assert job is not None
        return job.status
    return execution.status


def proposal_out(execution, proposal, decision, job) -> ProposalOut:
    """Expose only public proposal fields with the continuation status once approved.
    只公开方案的公开字段；批准后用恢复待办的状态表示执行进度。
    """
    return ProposalOut(
        id=proposal.id,
        version=PROPOSAL_VERSION,
        status=_execution_status(execution, job),
        ticket_id=str(proposal.ticket_id),
        order_id=str(execution.requested_order_id),
        refund_amount_minor=str(execution.refund_amount_minor),
        currency=execution.currency,
        customer_reply=proposal.customer_reply,
        proposed_ticket_status=proposal.proposed_ticket_status,
        policy_version=execution.policy_version,
        delivered_at=proposal.expected_order_facts["delivered_at"],
        checked_at=execution.checked_at,
        expires_at=proposal.expires_at,
        decision=decision.decision if decision else None,
        decided_at=decision.decided_at if decision else None,
        resume_job_id=decision.resume_job_id if decision else None,
        revalidated_at=execution.revalidated_at,
        invalidation_reason=execution.invalidation_reason,
    )


def read_history(session: Session, conversation_id: UUID, after_seq: int, limit: int) -> list[MessageOut]:
    """Read one ordered page after a sequence number; callers must check ownership first.
    读取指定顺序号之后的一页有序消息；调用方必须先完成会话归属检查。
    """
    rows = session.execute(
        _history_query(conversation_id).where(Message.seq > after_seq).order_by(Message.seq).limit(limit)
    ).all()
    activity = read_activity(session, [message.id for message, *_ in rows if message.role == "user"])
    history = []
    for message, execution, proposal, decision, job in rows:
        result = MessageOut.model_validate(message)
        if message.id in activity:
            result.activity = ActivityOut.model_validate(activity[message.id])
        # Attach saved execution IDs to their source user messages.
        # 将已保存的执行记录编号附在对应的用户消息上。
        if message.role == "user" and execution is not None:
            result.execution_id = execution.id
        if proposal is not None:
            result.proposal = proposal_out(execution, proposal, decision, job)
        history.append(result)
    return history


def read_history_snapshot(
    session: Session, conversation_id: UUID, limit: int
) -> tuple[list[MessageOut], int | None]:
    """Read the first page and its cursor inside the caller's repeatable-read transaction.
    在调用方的可重复读事务中读取历史首屏及其游标。
    """
    # The cursor must describe the same database snapshot as the page.
    # 游标必须对应历史首屏使用的同一个数据库快照。
    messages = read_history(session, conversation_id, 0, limit)
    return messages, read_event_head(session, conversation_id)


def read_proposals(session: Session, conversation_id: UUID) -> list[ProposalOut]:
    """Read every proposal in one conversation so the stream can detect status changes.
    读取会话内全部方案，供事件流检测状态变化；单个会话中的方案数量很少。
    """
    rows = session.execute(
        _history_query(conversation_id).where(ResolutionProposal.id.is_not(None)).order_by(Message.seq)
    )
    return [
        proposal_out(execution, proposal, decision, job) for _, execution, proposal, decision, job in rows
    ]


def read_execution_detail(session: Session, execution_id: UUID, user_id: UUID) -> ExecutionDetailOut | None:
    """Read one owned execution and its optional workflow records in a single statement.
    用一条查询读取当前账号的执行记录及其可选流程数据。
    """
    row = session.execute(
        select(Execution, ResolutionProposal, ProposalDecision, Job, AppliedResolution)
        .select_from(Execution)
        .join(Message, Message.id == Execution.source_message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .outerjoin(ProposalDecision, ProposalDecision.proposal_id == ResolutionProposal.id)
        .outerjoin(Job, Job.id == ProposalDecision.resume_job_id)
        .outerjoin(AppliedResolution, AppliedResolution.proposal_id == ProposalDecision.proposal_id)
        .where(
            Execution.id == execution_id,
            Message.role == "user",
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    ).one_or_none()
    if row is None:
        return None

    execution, proposal, decision, job, applied = row
    amount = execution.refund_amount_minor
    result = None
    if applied is not None:
        result = AppliedResolutionOut(
            order_id=str(applied.order_id),
            ticket_id=str(applied.ticket_id),
            refund_amount_minor=str(applied.refund_amount_minor),
            currency=applied.currency,
            customer_reply=applied.customer_reply,
            applied_at=applied.applied_at,
        )
    return ExecutionDetailOut(
        id=execution.id,
        source_message_id=execution.source_message_id,
        order_id=str(execution.requested_order_id),
        outcome=execution.outcome,
        reason_codes=execution.reason_codes,
        refund_amount_minor=str(amount) if amount is not None else None,
        currency=execution.currency,
        policy_version=execution.policy_version,
        checked_at=execution.checked_at,
        status=_execution_status(execution, job),
        proposal_id=proposal.id if proposal else None,
        expires_at=proposal.expires_at if proposal else None,
        revalidated_at=execution.revalidated_at,
        invalidation_reason=execution.invalidation_reason,
        decision=ProposalDecisionOut.model_validate(decision) if decision else None,
        resume_job_status=job.status if job else None,
        result=result,
    )
