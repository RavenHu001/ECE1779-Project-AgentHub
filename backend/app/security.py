# Authenticate development accounts with HTTP Basic and salted password hashes.
# 使用 HTTP Basic 和加盐密码哈希验证开发账号。

import base64
import hashlib
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

basic = HTTPBasic()


# Store the algorithm, random salt and digest; never the original password.
# 保存算法、随机盐和摘要，不保存原始密码。
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


# Compare the derived digest in constant time and reject malformed hashes.
# 以恒定时间比较派生摘要，并拒绝格式无效的哈希。
def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1)
        return secrets.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# Perform a hash check even for unknown accounts to reduce timing differences.
# 即使账号不存在也校验一次哈希，减少时间差异。
_dummy_hash = hash_password(secrets.token_urlsafe(32))


# Load a trusted account from credentials; never accept an owner ID from the client.
# 根据凭据加载可信账号，不信任客户端传入的归属用户编号。
def current_user(credentials: HTTPBasicCredentials = Depends(basic)) -> User:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == credentials.username.strip().lower()))
        valid = verify_password(credentials.password, user.password_hash if user else _dummy_hash)
        if not user or not valid or not user.is_active:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return user
