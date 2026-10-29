# Run versioned migrations offline or against the configured database.
# 离线生成迁移 SQL，或对配置的数据库执行版本化迁移。

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import database_url
from app.models import Base

if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
