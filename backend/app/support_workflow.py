"""Stage account-scoped refund checks and proposals for the conversation worker.
为会话 worker 暂存按账号授权的退款检查与方案。
"""

import re
from datetime import UTC, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AppliedResolution, Execution, Order, ResolutionProposal
from app.refund_eligibility import (
    POLICY_VERSION,
    REFUND_WINDOW,
    RefundEligibility,
    evaluate_refund_eligibility,
    format_refund_reply,
    parse_refund_order_id,
)
from app.ticket_lookup import MAX_TICKET_ID, lookup_ticket, parse_ticket_id

# Fixed mode translates explicit requests into the same action a future agent can propose.
# 固定模式将明确的请求转换为未来 Agent 也可提出的同一结构化动作。
PREPARE_REQUEST = re.compile(
    r"Prepare a refund for ticket #?([0-9]{1,19})|处理工单 #?([0-9]{1,19})(?:，符合条件就准备退款)?"
)


class PrepareRefund(BaseModel):
    """Accept a target and draft text while the worker supplies authority and computes facts.
    接收工单目标和回复草稿，由 worker 提供授权身份并计算业务事实。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    ticket_id: int = Field(strict=True, gt=0, le=MAX_TICKET_ID)
    customer_reply: str | None = Field(default=None, max_length=20000)

    @field_validator("customer_reply")
    @classmethod
    def check_customer_reply(cls, value: str | None) -> str | None:
        """Reject unusable drafts while preserving the original wording of valid drafts.
        拒绝不可用草稿，保留合法草稿的原始措辞。
        """
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Customer reply must contain text and cannot contain null characters")
        return value


def parse_prepare_refund(content: str) -> PrepareRefund | None:
    """Translate one complete fixed-mode request into a validated action.
    将一个完整的固定模式请求转换为已校验的动作。
    """
    match = PREPARE_REQUEST.fullmatch(content.strip())
    if match is None:
        return None
    # The regex bounds the number of digits, and the action enforces the exact BIGINT range.
    # 正则限制数字位数，动作类型进一步检查 BIGINT 的精确范围。
    ticket_id = int(match[1] or match[2])
    return PrepareRefund(ticket_id=ticket_id) if 0 < ticket_id <= MAX_TICKET_ID else None


def _record_order_check(
    session: Session, source_id: UUID, order_id: int, order: Order | None
) -> tuple[Execution, RefundEligibility]:
    """Evaluate and stage the common eligibility record inside the caller's transaction.
    在调用方事务内计算并暂存共用的资格记录。
    """
    # Use one database time for the policy decision and its durable evidence.
    # 资格判断和持久化依据使用同一个数据库时刻。
    checked_at = session.scalar(select(func.clock_timestamp()))
    result = evaluate_refund_eligibility(order, checked_at)
    if (
        order is not None
        and session.scalar(
            select(AppliedResolution.proposal_id).where(AppliedResolution.order_id == order.id)
        )
        is not None
    ):
        result = RefundEligibility("INELIGIBLE", ("ALREADY_REFUNDED",), currency=result.currency)
    execution = Execution(
        source_message_id=source_id,
        requested_order_id=order_id,
        outcome=result.outcome,
        reason_codes=list(result.reason_codes),
        refund_amount_minor=result.refund_amount_minor,
        currency=result.currency,
        policy_version=POLICY_VERSION,
        checked_at=checked_at,
    )
    session.add(execution)
    return execution, result


def order_facts(order: Order) -> dict:
    """Capture the policy inputs in the same format for planning and application.
    在准备与执行阶段以同一格式提取退款规则使用的订单资料。
    """
    return {
        "payment_status": order.payment_status,
        "fulfillment_status": order.fulfillment_status,
        "paid_total_minor": order.paid_total_minor,
        "currency": order.currency,
        "delivered_at": order.delivered_at.astimezone(UTC).isoformat() if order.delivered_at else None,
    }


def check_demo_refund_request(session: Session, source_id: UUID, content: str, user_id: UUID) -> str | None:
    """Return the read-only reply for a documented order or ticket request, if matched.
    匹配文档约定的订单或工单请求时，返回只读查询回复。
    """
    # Use the owner of the stored conversation for both kinds of lookup.
    # 两种查询均使用已保存会话的所有者身份。
    ticket_id = parse_ticket_id(content)
    prefix = ""
    if ticket_id is not None:
        match = lookup_ticket(session, user_id, ticket_id)
        if match is None:
            return "Ticket not found."
        status, order = match
        prefix = f"Ticket {ticket_id} is {status.lower()}. "
        if order is None:
            return prefix + "More information is required. No accessible order is linked to this ticket."
        order_id = order.id
    else:
        order_id = parse_refund_order_id(content)
        if order_id is None:
            return None
        order = session.scalar(select(Order).where(Order.id == order_id, Order.assigned_user_id == user_id))

    _, result = _record_order_check(session, source_id, order_id, order)
    return prefix + format_refund_reply(order_id, result)


def prepare_refund(session: Session, source_id: UUID, user_id: UUID, action: PrepareRefund) -> str | None:
    """Stage a proposal and return None, or return a read-only explanation when blocked.
    暂存方案并返回 None；无法准备方案时，返回只读原因说明。
    """
    # Resolve the account-scoped facts before evaluating any model- or user-proposed action.
    # 处理用户或模型提出的动作前，先读取当前账号有权访问的业务资料。
    match = lookup_ticket(session, user_id, action.ticket_id)
    if match is None:
        return "Ticket not found."
    status, order = match
    if status != "OPEN":
        return f"Ticket {action.ticket_id} is already resolved."
    if order is None:
        return f"More information is required for ticket {action.ticket_id}. No accessible order is linked."

    execution, result = _record_order_check(session, source_id, order.id, order)
    if result.outcome != "ELIGIBLE":
        return format_refund_reply(order.id, result)

    # Limit the approval window to one day and to the remaining policy window.
    # 方案有效期最多一天，且不得超过退款政策剩余期限。
    delivered_at = order.delivered_at.astimezone(UTC)
    expires_at = min(execution.checked_at.astimezone(UTC) + timedelta(days=1), delivered_at + REFUND_WINDOW)
    if expires_at <= execution.checked_at:
        return "The refund window has ended for preparing a proposal."

    if action.customer_reply is None:
        dollars, cents = divmod(result.refund_amount_minor, 100)
        customer_reply = f"Your order is eligible for a refund of {result.currency} {dollars}.{cents:02d}."
    else:
        customer_reply = action.customer_reply

    # Store the exact approved-action inputs and the facts that will be checked before execution.
    # 固定待审批动作的具体内容，并保存执行前需要重新核验的业务事实。
    execution.status = "WAITING_FOR_APPROVAL"
    session.flush([execution])
    session.add(
        ResolutionProposal(
            execution_id=execution.id,
            ticket_id=action.ticket_id,
            customer_reply=customer_reply,
            proposed_ticket_status="RESOLVED",
            expected_order_facts=order_facts(order),
            expires_at=expires_at,
        )
    )
    return None
