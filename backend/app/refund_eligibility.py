"""Parse demo requests and evaluate the read-only refund policy.
识别演示请求并计算只读退款资格。
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.models import Order

POLICY_VERSION = "DEMO-CAD-30D/v1"
REFUND_WINDOW = timedelta(days=30)
MAX_ORDER_ID = 2**63 - 1

# Match only the documented forms and bound the integer before converting it.
# 仅匹配文档中的完整句式，并在转换前限制数字长度。
CHECK_REQUEST = re.compile(r"Can order ([0-9]{1,19}) be refunded\?|订单 ([0-9]{1,19}) 能退款吗？")

REASON_TEXT = {
    "NOT_PAID": "The order has not been paid.",
    "NOT_DELIVERED": "The order has not been delivered.",
    "UNSUPPORTED_CURRENCY": "The demo policy only supports CAD.",
    "WINDOW_EXPIRED": "The 30-day refund window has expired.",
    "MISSING_PAYMENT_STATUS": "The payment status is missing.",
    "MISSING_FULFILLMENT_STATUS": "The delivery status is missing.",
    "MISSING_CURRENCY": "The currency is missing.",
    "INVALID_CURRENCY": "The currency code is invalid.",
    "INVALID_PAID_AMOUNT": "A valid paid amount is required.",
    "MISSING_DELIVERED_AT": "The delivery date is missing.",
    "INVALID_DELIVERED_AT": "The delivery date is in the future.",
}


@dataclass(frozen=True)
class RefundEligibility:
    """Keep the structured decision independent of its chat wording.
    将结构化判断与聊天回复的表述分开。
    """

    outcome: Literal["ELIGIBLE", "INELIGIBLE", "NEEDS_INFORMATION", "NOT_FOUND"]
    reason_codes: tuple[str, ...] = ()
    refund_amount_minor: int | None = None
    currency: str | None = None


def parse_refund_order_id(content: str) -> int | None:
    """Return the positive BIGINT ID from a complete demo request.
    从完整的演示请求中提取正整数订单号，并限制在 BIGINT 范围内。
    """
    match = CHECK_REQUEST.fullmatch(content.strip())
    if match is None:
        return None
    order_id = int(match[1] or match[2])
    return order_id if 0 < order_id <= MAX_ORDER_ID else None


def evaluate_refund_eligibility(order: Order | None, checked_at: datetime) -> RefundEligibility:
    """Apply the demo policy to a visible order and a single UTC check time.
    使用当前用户可见的订单和一次 UTC 检查时间，计算演示退款资格。
    """
    if order is None:
        return RefundEligibility("NOT_FOUND", ("ORDER_NOT_FOUND",))

    # Collect known denials separately so missing facts do not obscure them.
    # 单独收集明确的拒绝原因，避免缺失资料掩盖已知结论。
    denials = []
    missing = []
    if order.payment_status == "UNPAID":
        denials.append("NOT_PAID")
    elif order.payment_status is None:
        missing.append("MISSING_PAYMENT_STATUS")
    if order.fulfillment_status == "NOT_DELIVERED":
        denials.append("NOT_DELIVERED")
    elif order.fulfillment_status is None:
        missing.append("MISSING_FULFILLMENT_STATUS")

    currency = order.currency
    if currency is None:
        missing.append("MISSING_CURRENCY")
    elif not re.fullmatch(r"[A-Z]{3}", currency):
        missing.append("INVALID_CURRENCY")
        currency = None
    elif currency != "CAD":
        denials.append("UNSUPPORTED_CURRENCY")
    if order.paid_total_minor is None or order.paid_total_minor <= 0:
        missing.append("INVALID_PAID_AMOUNT")

    # Compare UTC instants so the 30-day window is exactly 30 times 24 hours.
    # 比较 UTC 时刻，确保退款窗口是精确的 30 个 24 小时。
    if order.delivered_at is None:
        missing.append("MISSING_DELIVERED_AT")
    else:
        age = checked_at.astimezone(UTC) - order.delivered_at.astimezone(UTC)
        if age < timedelta(0):
            missing.append("INVALID_DELIVERED_AT")
        elif age > REFUND_WINDOW:
            denials.append("WINDOW_EXPIRED")

    if denials:
        return RefundEligibility("INELIGIBLE", tuple(denials), currency=currency)
    if missing:
        return RefundEligibility("NEEDS_INFORMATION", tuple(missing), currency=currency)
    return RefundEligibility("ELIGIBLE", refund_amount_minor=order.paid_total_minor, currency=currency)


def format_refund_reply(order_id: int, result: RefundEligibility) -> str:
    """Build an English chat reply from the structured decision and reasons.
    根据结构化判断与原因生成英文聊天回复。
    """
    if result.outcome == "NOT_FOUND":
        return "Order not found."
    if result.outcome == "ELIGIBLE":
        # Keep cents exact even for amounts too large for precise floats.
        # 用整数计算保留精确到分的金额，避免大额数据的浮点误差。
        dollars, cents = divmod(result.refund_amount_minor, 100)
        return f"Order {order_id} is eligible for a simulated refund of CAD {dollars}.{cents:02d}."
    reasons = " ".join(REASON_TEXT[code] for code in result.reason_codes)
    if result.outcome == "INELIGIBLE":
        return f"Order {order_id} is not eligible for a simulated refund. {reasons}"
    return f"More information is required to check order {order_id}. {reasons}"
