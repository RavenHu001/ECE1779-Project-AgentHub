"""Add audited decisions and transactional simulated resolutions.
添加可审计的审批决定与事务化模拟处置。
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_approved_resolutions"
down_revision = "0005_resolution_proposals"
branch_labels = None
depends_on = None


def upgrade():
    """Allow an approved continuation while keeping existing messages and proposals.
    允许获批后的恢复待办，并保留已有消息与方案。
    """
    # Existing jobs retain their original kind and remain unique per source.
    # 已有待办保留原类型，并继续按来源保持唯一。
    op.drop_constraint("uq_jobs_source", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_source_kind", "jobs", ["source_message_id", "kind"])
    op.drop_constraint("ck_jobs_kind", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_kind", "jobs", "kind IN ('PROCESS_MESSAGE', 'RESUME_EXECUTION')")
    op.drop_constraint("ck_executions_status", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_status",
        "executions",
        "status = 'COMPLETED' OR (outcome = 'ELIGIBLE' AND "
        "status IN ('WAITING_FOR_APPROVAL', 'APPROVED', 'REJECTED', 'INVALIDATED'))",
    )

    # Persist the exact decision receipt and enforce one applied resolution per order and ticket.
    # 保存审批原始回执，并约束每笔订单和每张工单只能应用一次处置。
    op.create_table(
        "proposal_decisions",
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("resolution_proposals.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resume_job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), unique=True),
        sa.CheckConstraint(
            "(decision = 'APPROVE' AND resume_job_id IS NOT NULL) OR "
            "(decision = 'REJECT' AND resume_job_id IS NULL)",
            name="ck_proposal_decisions_resume",
        ),
    )
    op.create_table(
        "applied_resolutions",
        sa.Column(
            "proposal_id", sa.Uuid(), sa.ForeignKey("proposal_decisions.proposal_id"), primary_key=True
        ),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), unique=True, nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), sa.ForeignKey("tickets.id"), unique=True, nullable=False),
        sa.Column("refund_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("customer_reply", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "refund_amount_minor > 0 AND currency = 'CAD'", name="ck_applied_resolutions_refund"
        ),
        sa.CheckConstraint("customer_reply ~ '[^[:space:]]'", name="ck_applied_resolutions_reply"),
    )


def downgrade():
    """Restore the planning-only schema after removing continuation records.
    移除恢复执行记录后，还原仅支持方案准备的结构。
    """
    # Refuse to discard audited decisions or applied business history.
    # 拒绝丢弃已审计的审批决定或已应用的业务历史。
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM proposal_decisions) OR "
            "EXISTS (SELECT 1 FROM executions WHERE status = 'INVALIDATED')"
        )
    ):
        raise RuntimeError("Cannot downgrade while proposal decisions or invalidations exist")
    op.drop_table("applied_resolutions")
    op.drop_table("proposal_decisions")
    op.drop_constraint("ck_executions_status", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_status",
        "executions",
        "status = 'COMPLETED' OR (status = 'WAITING_FOR_APPROVAL' AND outcome = 'ELIGIBLE')",
    )
    op.drop_constraint("ck_jobs_kind", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_kind", "jobs", "kind = 'PROCESS_MESSAGE'")
    op.drop_constraint("uq_jobs_source_kind", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_source", "jobs", ["source_message_id"])
