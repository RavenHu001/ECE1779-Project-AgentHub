# Share the connection pool, while keeping sessions scoped to each operation.
# 共用连接池，但每次操作使用独立会话。

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import database_url

engine = create_engine(database_url(), pool_pre_ping=True, connect_args={"options": "-c timezone=UTC"})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# Release the request session even when a handler raises an exception.
# 即使接口抛出异常，也释放请求使用的数据库会话。
def get_session():
    with SessionLocal() as session:
        yield session
