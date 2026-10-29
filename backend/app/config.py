# Load database and worker configuration from the container environment.
# 从容器环境变量读取数据库与 worker 配置。

import os

from sqlalchemy import URL


# Construct the connection URL without manually escaping credentials.
# 构造连接地址，避免手工拼接和转义凭据。
def database_url() -> URL:
    # A URL object handles punctuation in passwords.
    # URL 对象会处理密码中的特殊标点。
    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("PGUSER", "agenthub"),
        password=os.getenv("PGPASSWORD", ""),
        host=os.getenv("PGHOST", "db"),
        port=int(os.getenv("PGPORT", "5432")),
        database=os.getenv("PGDATABASE", "agenthub"),
    )


# Reject invalid timing limits before starting a worker.
# 在启动 worker 前拒绝无效的时间限制。
def positive_number(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
