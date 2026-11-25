"""Exercise browser sessions against the real API and isolated PostgreSQL.
通过真实 API 与隔离 PostgreSQL 验证浏览器会话。
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.manage import seed_demo_orders
from app.models import AuthSession, User
from app.security import SESSION_COOKIE, digest, hash_password
from app.worker import run_once

HEADERS = {"X-AgentHub-Request": "1"}


def login(client, username="alice", password="alice-test-password"):
    client.auth = None
    return client.post("/auth/login", json={"username": username, "password": password}, headers=HEADERS)


def test_login_survives_new_browser_client_with_absolute_24_hour_expiry(client):
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=86400" in cookie
    assert response.headers["cache-control"] == "no-store"
    token = client.cookies[SESSION_COOKIE]
    with SessionLocal() as session:
        saved = session.get(AuthSession, digest(token))
        assert saved is not None and saved.token_hash != token
        expiry = saved.expires_at
        assert 86398 < (expiry - saved.created_at).total_seconds() < 86402
    with TestClient(app) as reopened:
        reopened.cookies.set(SESSION_COOKIE, token)
        assert reopened.get("/me").json()["username"] == "alice"
        assert reopened.get("/me").headers["cache-control"] == "no-store"
        assert reopened.post("/conversations", json={}, headers=HEADERS).status_code == 201
    with SessionLocal() as session:
        assert session.get(AuthSession, digest(token)).expires_at == expiry


def test_cloud_sets_secure_cookie_even_behind_tls_proxy(client, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    response = login(client)
    assert "; Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("header", [None, "Basic invalid!", "Bearer invalid", "Basic YWxpY2U6d3Jvbmc="])
def test_auth_failures_never_trigger_browser_basic_popup(client, header):
    client.auth = None
    response = client.get("/me", headers={} if header is None else {"Authorization": header})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_failed_login_preserves_existing_session_without_issuing_another(client):
    assert login(client).status_code == 200
    old_token = client.cookies[SESSION_COOKIE]
    response = login(client, password="wrong")
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers and "set-cookie" not in response.headers
    assert client.cookies[SESSION_COOKIE] == old_token


def test_login_requires_header_and_rotates_only_this_browser_session(client):
    client.auth = None
    payload = {"username": "alice", "password": "alice-test-password"}
    assert client.post("/auth/login", json=payload).status_code == 403
    assert login(client).status_code == 200
    old_token = client.cookies[SESSION_COOKIE]
    with TestClient(app) as second_device:
        assert login(second_device).status_code == 200
        second_token = second_device.cookies[SESSION_COOKIE]
        assert login(client).status_code == 200
        assert client.cookies[SESSION_COOKIE] != old_token
        assert second_device.get("/me").status_code == 200
    with SessionLocal() as session:
        assert session.get(AuthSession, digest(old_token)) is None
        assert session.get(AuthSession, digest(second_token)) is not None


def test_cookie_writes_require_header_and_cannot_cross_account_context(client):
    alice = login(client).json()
    assert client.post("/conversations", json={}).status_code == 403
    conversation = client.post("/conversations", json={}, headers=HEADERS).json()["id"]
    bob = login(client, "bob", "bob-test-password").json()
    stale = HEADERS | {"X-AgentHub-Account": alice["id"]}
    assert client.post("/conversations", json={}, headers=stale).status_code == 401
    assert client.get("/conversations", headers=stale).status_code == 401
    assert client.get(f"/conversations/{conversation}/messages").status_code == 404
    assert client.get("/me", headers={"X-AgentHub-Account": bob["id"]}).status_code == 200


@pytest.mark.parametrize("reason", ["expired", "disabled", "password_changed", "tampered"])
def test_expiry_and_revocation_are_enforced_on_server(client, reason):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    with SessionLocal.begin() as session:
        if reason == "expired":
            session.get(AuthSession, digest(token)).expires_at = session.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
        elif reason == "disabled":
            session.scalar(select(User).where(User.username == "alice")).is_active = False
        elif reason == "password_changed":
            session.scalar(select(User).where(User.username == "alice")).password_hash = hash_password("new")
    if reason == "tampered":
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, token + "tampered")
    assert client.get("/me").status_code == 401
    assert client.post("/conversations", json={}, headers=HEADERS).status_code == 401


def test_logout_revokes_token_and_clears_cookie(client):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    assert client.post("/auth/logout").status_code == 403
    response = client.post("/auth/logout", headers=HEADERS)
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/me").status_code == 401
    assert client.post("/auth/logout", headers=HEADERS).status_code == 204


def test_explicit_basic_api_auth_still_works_and_invalid_auth_does_not_fall_back(client):
    login(client)
    assert client.get("/me", auth=("bob", "bob-test-password")).json()["username"] == "bob"
    assert client.get("/me", auth=("bob", "wrong")).status_code == 401
    assert client.post("/conversations", json={}, auth=("alice", "alice-test-password")).status_code == 201


def test_cookie_login_preserves_human_approval_and_duplicate_submission(client):
    seed_demo_orders("alice", "bob", with_tickets=True)
    login(client)
    client.headers.update(HEADERS)
    conversation = client.post("/conversations", json={}).json()["id"]
    path = f"/conversations/{conversation}/messages"
    payload = {"content": "Prepare a refund for ticket 391", "submission_key": str(uuid4())}
    receipt = client.post(path, json=payload)
    assert receipt.status_code == 202
    assert client.post(path, json=payload).json() == receipt.json()
    assert run_once("cookie-planning-worker")
    message = client.get(path).json()[0]
    proposal = message["proposal"]
    detail_path = f"/executions/{message['execution_id']}"
    assert client.get(detail_path).json()["status"] == "WAITING_FOR_APPROVAL"
    assert client.get(detail_path).json()["result"] is None
    decision = {"decision": "APPROVE", "version": proposal["version"]}
    url = f"/proposals/{proposal['id']}/decision"
    client.headers.pop("X-AgentHub-Request")
    assert client.post(url, json=decision).status_code == 403
    client.headers.update(HEADERS)
    approved = client.post(url, json=decision)
    assert approved.status_code == 200
    assert client.post(url, json=decision).json() == approved.json()
    assert run_once("cookie-refund-worker")
    assert client.get(detail_path).json()["status"] == "COMPLETED"
    assert client.get(detail_path).json()["result"] is not None


def test_open_event_stream_stops_after_session_revocation(client):
    from app.events import conversation_events
    from app.security import session_user

    login(client)
    token_hash = digest(client.cookies[SESSION_COOKIE])

    class Stream:
        def poll(self):
            raise AssertionError("A revoked stream must not read any conversation data")

    async def disconnected():
        return False

    async def run():
        stream = conversation_events(Stream(), disconnected, lambda: session_user(token_hash) is not None)
        assert await anext(stream) == "retry: 2000\n\n"
        assert client.post("/auth/logout", headers=HEADERS).status_code == 204
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run())
