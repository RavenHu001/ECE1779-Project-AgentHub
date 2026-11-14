"""Recognize fixed-mode ticket requests and load account-scoped business facts.
识别固定模式下的工单请求，并按账号读取业务资料。
"""

import re
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Order, Ticket

# Require a complete request containing a single ASCII ticket number.
# 只接受包含一个 ASCII 工单号的完整查询句式。
TICKET_REQUEST = re.compile(r"Look up ticket #?([0-9]{1,19})|查询工单 #?([0-9]{1,19})")
MAX_TICKET_ID = 2**63 - 1


def parse_ticket_id(content: str) -> int | None:
    """Return a positive BIGINT ticket number from a supported query.
    从支持的查询句式中提取 BIGINT 范围内的正整数工单号。
    """
    match = TICKET_REQUEST.fullmatch(content.strip())
    if match is None:
        return None
    # The pattern bounds the digit count before conversion. Enforce the exact database range.
    # 句式在转换前限制位数，此处进一步校验数据库的精确取值范围。
    ticket_id = int(match[1] or match[2])
    return ticket_id if 0 < ticket_id <= MAX_TICKET_ID else None


def lookup_ticket(session: Session, user_id: UUID, ticket_id: int) -> tuple[str, Order | None] | None:
    """Fetch a visible ticket's status and its visible linked order in one query.
    在同一次查询中取得当前账号可见的工单状态及其可见的关联订单。
    """
    # Filter order ownership in the join so inaccessible orders look like absent links.
    # 在连接条件中检查订单归属，使无权访问的订单与未关联订单表现一致。
    row = session.execute(
        select(Ticket.status, Order)
        .outerjoin(Order, and_(Order.id == Ticket.order_id, Order.assigned_user_id == user_id))
        .where(Ticket.id == ticket_id, Ticket.assigned_user_id == user_id)
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None
