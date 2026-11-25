# Authenticate development accounts with HTTP Basic and salted password hashes.
# 使用 HTTP Basic 和加盐密码哈希验证开发账号。

import base64
import hashlib
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AuthSession, User

SESSION_COOKIE = "agenthub_session"
SESSION_SECONDS = 24 * 60 * 60


class QuietHTTPBasic(HTTPBasic):
    """Preserve API clients without asking the browser to open a second login dialog.
    保留 API 客户端认证，同时避免浏览器弹出第二个登录框。
    """

    async def __call__(self, request: Request) -> HTTPBasicCredentials | None:
        try:
            return await super().__call__(request)
        except HTTPException:
            raise HTTPException(401, "Invalid credentials") from None


basic = QuietHTTPBasic(auto_error=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_browser_request(request: Request):
    """Require a non-simple header so cross-site forms cannot use browser cookies.
    要求非简单请求头，防止跨站表单使用浏览器 cookie 执行操作。
    """
    if request.headers.get("X-AgentHub-Request") != "1":
        raise HTTPException(403, "Browser request header required")


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
def authenticate(username: str, password: str) -> User:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username.strip().lower()))
        valid = verify_password(password, user.password_hash if user else _dummy_hash)
        if not user or not valid or not user.is_active:
            raise HTTPException(401, "Invalid credentials")
        return user


def session_user(token_hash: str) -> User | None:
    with SessionLocal() as session:
        row = session.execute(
            select(User, AuthSession.password_fingerprint)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == token_hash,
                AuthSession.expires_at > func.clock_timestamp(),
                User.is_active.is_(True),
            )
        ).one_or_none()
        if row is None or not secrets.compare_digest(row[1], digest(row[0].password_hash)):
            return None
        return row[0]


def current_user(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)) -> User:
    # Explicit API credentials take precedence and never fall back to an unrelated browser session.
    # 显式 API 凭据优先，失败时不会退回到无关的浏览器会话。
    if request.headers.get("Authorization") is not None:
        if credentials is None:
            raise HTTPException(401, "Invalid credentials")
        return authenticate(credentials.username, credentials.password)
    token = request.cookies.get(SESSION_COOKIE, "")
    user = session_user(digest(token)) if token and len(token) <= 256 else None
    if user is None:
        raise HTTPException(401, "Sign in required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_browser_request(request)
    expected = request.headers.get("X-AgentHub-Account")
    if expected is not None and expected != str(user.id):
        raise HTTPException(401, "Account changed; sign in again")
    request.state.session_hash = digest(token)
    return user
