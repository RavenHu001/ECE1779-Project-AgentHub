"""Browser login with a revocable, shared 24-hour session.
浏览器登录使用可撤销、跨副本共享的 24 小时会话。
"""

import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import AuthSession
from app.security import SESSION_COOKIE, SESSION_SECONDS, authenticate, digest, require_browser_request

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


def secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"


@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response):
    require_browser_request(request)
    user = authenticate(payload.username, payload.password)
    token = secrets.token_urlsafe(32)
    previous = request.cookies.get(SESSION_COOKIE, "")
    with SessionLocal.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        # Rotate the current browser's token and discard expired records without touching other devices.
        # 轮换当前浏览器令牌并清理过期记录，不影响其他设备。
        session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        if previous:
            session.execute(delete(AuthSession).where(AuthSession.token_hash == digest(previous)))
        session.add(
            AuthSession(
                token_hash=digest(token),
                user_id=user.id,
                password_fingerprint=digest(user.password_hash),
                expires_at=now + timedelta(seconds=SESSION_SECONDS),
            )
        )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        expires=now + timedelta(seconds=SESSION_SECONDS),
        httponly=True,
        secure=secure_cookie(request),
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    require_browser_request(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with SessionLocal.begin() as session:
            session.execute(delete(AuthSession).where(AuthSession.token_hash == digest(token)))
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, secure=secure_cookie(request), samesite="lax", path="/"
    )
    response.headers["Cache-Control"] = "no-store"
