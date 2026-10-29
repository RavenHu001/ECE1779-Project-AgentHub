# Preserve the historical initial SQL; later revisions change defaults without rewriting it.
# 保留历史初始化 SQL；之后通过新迁移修改默认值，不重写原有 SQL。

"""Create the four initial tables. SQL is frozen independently of future model edits."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# Create tables and indexes in dependency order using frozen SQL.
# 使用已固定的 SQL 按依赖顺序建立表和索引。
def upgrade():
    op.execute("""
CREATE TABLE users (
	id UUID NOT NULL,
	username TEXT NOT NULL,
	display_name TEXT,
	password_hash TEXT NOT NULL,
	is_active BOOLEAN DEFAULT true NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_users_username UNIQUE (username),
	CONSTRAINT ck_users_username CHECK (username = lower(btrim(username)) AND username ~ '[^[:space:]]')
)
""")
    op.execute("""
CREATE TABLE conversations (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	title TEXT DEFAULT '新对话' NOT NULL,
	next_message_seq BIGINT DEFAULT 1 NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_conversations_seq CHECK (next_message_seq > 0),
	CONSTRAINT ck_conversations_title CHECK (title ~ '[^[:space:]]'),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_conversations_user_updated ON conversations (user_id, updated_at DESC, id)
""")
    op.execute("""
CREATE TABLE messages (
	id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	role TEXT NOT NULL,
	content TEXT NOT NULL,
	seq BIGINT NOT NULL,
	reply_to_message_id UUID,
	submission_key UUID,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_messages_conversation_seq UNIQUE (conversation_id, seq),
	CONSTRAINT uq_messages_submission UNIQUE (conversation_id, submission_key),
	CONSTRAINT uq_messages_reply UNIQUE (reply_to_message_id),
	CONSTRAINT ck_messages_seq CHECK (seq > 0),
	CONSTRAINT ck_messages_content CHECK (content ~ '[^[:space:]]'),
	CONSTRAINT ck_messages_role_fields CHECK ((role = 'user' AND submission_key IS NOT NULL AND reply_to_message_id IS NULL) OR (role = 'assistant' AND submission_key IS NULL AND reply_to_message_id IS NOT NULL)),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE RESTRICT,
	FOREIGN KEY(reply_to_message_id) REFERENCES messages (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE TABLE jobs (
	id UUID NOT NULL,
	source_message_id UUID NOT NULL,
	kind TEXT DEFAULT 'PROCESS_MESSAGE' NOT NULL,
	status TEXT DEFAULT 'QUEUED' NOT NULL,
	attempts INTEGER DEFAULT 0 NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	lease_owner TEXT,
	lease_token UUID,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_jobs_source UNIQUE (source_message_id),
	CONSTRAINT ck_jobs_kind CHECK (kind = 'PROCESS_MESSAGE'),
	CONSTRAINT ck_jobs_attempts CHECK (attempts >= 0),
	CONSTRAINT ck_jobs_state CHECK ((status = 'QUEUED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NULL) OR (status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL AND attempts >= 1 AND finished_at IS NULL) OR (status = 'SUCCEEDED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL AND last_error IS NULL AND attempts >= 1) OR (status = 'FAILED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL AND last_error ~ '[^[:space:]]' AND last_error IS NOT NULL AND attempts >= 1)),
	FOREIGN KEY(source_message_id) REFERENCES messages (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_jobs_expired ON jobs (lease_expires_at) WHERE status = 'RUNNING'
""")
    op.execute("""
CREATE INDEX ix_jobs_ready ON jobs (status, available_at, created_at)
""")


# Drop dependent tables first when reverting an isolated development database.
# 回退隔离开发数据库时，先删除依赖其他表的表。
def downgrade():
    op.drop_table("jobs")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("users")
