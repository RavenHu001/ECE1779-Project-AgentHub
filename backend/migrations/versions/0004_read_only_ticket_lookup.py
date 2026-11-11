"""Add simulated tickets linked to existing demo orders.
添加关联已有演示订单的模拟工单。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_read_only_ticket_lookup"
down_revision = "0003_read_only_refund_check"
branch_labels = None
depends_on = None


def upgrade():
    """Create the ticket table without rewriting orders or chat history.
    创建工单表，保留已有订单和聊天历史。
    """
    # Keep the order reference optional so tickets can represent missing information.
    # 将订单关联设为可空，使工单能表示资料尚不完整的情况。
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "assigned_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="RESTRICT")),
        sa.Column("status", sa.Text(), server_default=sa.text("'OPEN'"), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_tickets_id"),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_tickets_status"),
    )


def downgrade():
    """Remove ticket storage while preserving order and chat data.
    删除工单表，保留订单和聊天数据。
    """
    op.drop_table("tickets")
