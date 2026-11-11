"""Add simulated orders and persisted refund eligibility results.
添加模拟订单与持久化退款资格结果。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_read_only_refund_check"
down_revision = "0002_english_defaults"
branch_labels = None
depends_on = None


def upgrade():
    """Create the new tables without rewriting existing chat records.
    创建新表，保留已有聊天记录。
    """
    # Demo orders are seeded after accounts exist, outside schema migration.
    # 模拟订单在账号创建后单独初始化。
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "assigned_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("payment_status", sa.Text()),
        sa.Column("fulfillment_status", sa.Text()),
        sa.Column("paid_total_minor", sa.BigInteger()),
        sa.Column("currency", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id > 0", name="ck_orders_id"),
        sa.CheckConstraint("payment_status IN ('PAID', 'UNPAID')", name="ck_orders_payment_status"),
        sa.CheckConstraint(
            "fulfillment_status IN ('DELIVERED', 'NOT_DELIVERED')", name="ck_orders_fulfillment_status"
        ),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("requested_order_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_codes", JSONB(), nullable=False),
        sa.Column("refund_amount_minor", sa.BigInteger()),
        sa.Column("currency", sa.Text()),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_message_id", name="uq_executions_source"),
        sa.CheckConstraint("requested_order_id > 0", name="ck_executions_requested_order"),
        sa.CheckConstraint(
            "outcome IN ('ELIGIBLE', 'INELIGIBLE', 'NEEDS_INFORMATION', 'NOT_FOUND')",
            name="ck_executions_outcome",
        ),
        sa.CheckConstraint("jsonb_typeof(reason_codes) = 'array'", name="ck_executions_reasons"),
        sa.CheckConstraint(
            "(outcome = 'ELIGIBLE' AND refund_amount_minor IS NOT NULL AND refund_amount_minor > 0 "
            "AND currency IS NOT NULL AND currency = 'CAD') OR "
            "(outcome <> 'ELIGIBLE' AND refund_amount_minor IS NULL)",
            name="ck_executions_refund_amount",
        ),
        sa.CheckConstraint("outcome <> 'NOT_FOUND' OR currency IS NULL", name="ck_executions_not_found"),
    )


def downgrade():
    """Remove the two milestone tables and retain the existing chat schema.
    删除本阶段新增的两张表，保留原有聊天表结构。
    """
    op.drop_table("executions")
    op.drop_table("orders")
