# Keep destructive integration-test fixtures confined to the isolated test database.
# 将会清理数据的集成测试夹具限制在隔离测试数据库中。

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import database_url
from app.db import SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password


# Refuse non-test databases before running migrations or cleanup.
# 在迁移或清理之前拒绝连接非测试数据库。
@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    if not database_url().database.endswith("_test"):
        pytest.exit("Refusing to run destructive fixtures outside a database ending in _test", returncode=2)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield
    engine.dispose()


# Reset only the dedicated test records between cases.
# 在测试案例之间仅重置专用测试记录。
@pytest.fixture(autouse=True)
def empty_database(migrated_database):
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs, messages, conversations, users CASCADE"))


# Create two accounts so ownership checks can be verified.
# 创建两个账号，以验证数据归属检查。
@pytest.fixture
def accounts():
    with SessionLocal.begin() as session:
        alice = User(username="alice", password_hash=hash_password("alice-test-password"))
        bob = User(username="bob", password_hash=hash_password("bob-test-password"))
        session.add_all([alice, bob])
        session.flush()
        return alice.id, bob.id


# Use the real authentication dependency through the HTTP test client.
# 通过 HTTP 测试客户端使用真实身份验证依赖。
@pytest.fixture
def client(accounts):
    with TestClient(app) as test_client:
        test_client.auth = ("alice", "alice-test-password")
        yield test_client
