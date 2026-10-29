"""Use an English default title without changing saved conversations.

将新会话的默认标题改为英文，保留已保存会话的原有标题。
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_english_defaults"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # Change only the default for future inserts; existing titles belong to users.
    # 只调整之后插入记录的默认值；已有标题属于用户数据。
    op.alter_column(
        "conversations", "title", existing_type=sa.Text(), server_default=sa.text("'New conversation'")
    )


def downgrade():
    # Restore the original migration's default, without rewriting historical data.
    # 恢复原迁移的默认值，不重写历史数据。
    op.alter_column("conversations", "title", existing_type=sa.Text(), server_default=sa.text("'新对话'"))
