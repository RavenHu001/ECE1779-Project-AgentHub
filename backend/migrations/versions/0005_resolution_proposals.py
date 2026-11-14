"""Persist refund proposals and track executions waiting for approval.
持久化退款方案，并记录正在等待审批的执行。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_resolution_proposals"
down_revision = "0004_read_only_ticket_lookup"
branch_labels = None
depends_on = None


def upgrade():
    """Keep existing eligibility checks completed and add storage for new proposals.
    将已有资格检查保留为已完成，并添加新方案的存储。
    """
    # The default backfills historical order checks without changing their results.
    # 通过默认值填充历史订单检查的状态，保留原有资格结论。
    op.add_column(
        "executions", sa.Column("status", sa.Text(), server_default=sa.text("'COMPLETED'"), nullable=False)
    )
    op.create_check_constraint(
        "ck_executions_status",
        "executions",
        "status = 'COMPLETED' OR (status = 'WAITING_FOR_APPROVAL' AND outcome = 'ELIGIBLE')",
    )

    # Each execution may expose a single immutable proposal.
    # 每次执行最多可展示一份不可编辑的方案。
    op.create_table(
        "resolution_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "execution_id", sa.Uuid(), sa.ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "ticket_id", sa.BigInteger(), sa.ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("customer_reply", sa.Text(), nullable=False),
        sa.Column("proposed_ticket_status", sa.Text(), nullable=False),
        sa.Column("expected_order_facts", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("execution_id", name="uq_resolution_proposals_execution"),
        sa.CheckConstraint("customer_reply ~ '[^[:space:]]'", name="ck_resolution_proposals_reply"),
        sa.CheckConstraint(
            "proposed_ticket_status = 'RESOLVED'", name="ck_resolution_proposals_ticket_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_order_facts) = 'object'", name="ck_resolution_proposals_facts"
        ),
    )


def downgrade():
    """Remove proposal storage and execution state while retaining eligibility results.
    删除方案存储及执行状态，保留已有资格结论。
    """
    op.drop_table("resolution_proposals")
    op.drop_constraint("ck_executions_status", "executions", type_="check")
    op.drop_column("executions", "status")
