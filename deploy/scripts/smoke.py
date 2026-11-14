#!/usr/bin/env python3
"""Verify the public API contract without importing backend/database internals."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4


class CheckFailed(Exception):
    """A diagnostic that is safe to print without server bodies or credentials."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailed("Redirect refused; verify the configured API URL")


def validate_url(value):
    url = urllib.parse.urlsplit(value)
    require(url.scheme in ("http", "https") and url.hostname, "Expected an HTTP(S) API URL")
    require(not url.username and not url.password, "Credentials must not appear in the URL")
    require(not url.query and not url.fragment and url.path in ("", "/"), "Use the API origin without a path, query or fragment")
    # Basic authentication over HTTP is permitted only through a loopback SSH tunnel.
    require(url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"), "Use HTTPS or a loopback SSH tunnel for authentication")
    return value.rstrip("/")


class Client:
    def __init__(self, base_url, username, password):
        self.base_url = validate_url(base_url)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.authorization = "Basic " + token
        # Do not forward deployment credentials through environment-configured proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, body=None, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == expected, "Unexpected API response status")
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise CheckFailed(f"API returned HTTP {error.code}; response body withheld") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise CheckFailed("API connection failed or response was not valid JSON") from None

    def history(self, conversation):
        result = []
        cursor = 0
        while True:
            page = self.call(f"/conversations/{conversation}/messages?limit=500&after_seq={cursor}")
            require(isinstance(page, list), "Expected a message list")
            for message in page:
                require(message["conversation_id"] == conversation and message["seq"] > cursor, "Conversation history is not ordered or belongs to another conversation")
                cursor = message["seq"]
                result.append(message)
            if len(page) < 500:
                return result
            require(len(result) <= 10000, "History exceeded the smoke check limit")


def fingerprint(messages):
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_record(handle, record):
    handle.seek(0)
    json.dump(record, handle, indent=2)
    handle.write("\n")
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def exercise(client, username, record_path, timeout):
    # Reserve the receipt before creating anything. Never overwrite a previous test.
    descriptor = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        record = {"version": 1, "status": "pending", "api_url": client.base_url, "username": username}
        write_record(handle, record)
        conversation = client.call("/conversations", {"title": "Deployment smoke check"}, expected=201)["id"]
        UUID(conversation)
        record.update(conversation_id=conversation, submission_key=str(uuid4()))
        write_record(handle, record)
        payload = {"content": "Hello", "submission_key": record["submission_key"]}
        path = f"/conversations/{conversation}/messages"
        receipt = client.call(path, payload, expected=202)
        UUID(receipt["job_id"])
        UUID(receipt["message_id"])
        record.update(job_id=receipt["job_id"], message_id=receipt["message_id"])
        write_record(handle, record)
        # Reuse the same idempotency contract exercised by backend/app/smoke.py.
        require(client.call(path, payload, expected=202) == receipt, "Duplicate submission returned a different receipt")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = client.call(f"/jobs/{receipt['job_id']}")
            require(job["status"] != "FAILED", "Worker reported failure; inspect its logs privately")
            if job["status"] == "SUCCEEDED":
                require(job["source_message_id"] == receipt["message_id"], "Job references a different message")
                break
            time.sleep(0.5)
        else:
            raise CheckFailed("Timed out waiting for job completion; receipt retained, no automatic resubmission")
        # The existing API exposes conversation metadata through its list endpoint.
        conversations = client.call("/conversations?limit=500")
        require(any(item["id"] == conversation for item in conversations), "Created conversation is absent from the recent conversation list")
        messages = client.history(conversation)
        require(len(messages) == 2, "Expected exactly one request and one reply")
        require(messages[0]["id"] == receipt["message_id"] and messages[0]["role"] == "user" and messages[0]["content"] == "Hello", "Saved request differs from the submission")
        require(messages[1]["role"] == "assistant" and messages[1]["reply_to_message_id"] == receipt["message_id"], "Reply does not match its request")
        digest = fingerprint(messages)
        require(fingerprint(client.history(conversation)) == digest, "History changed between independent API reads")
        record.update(status="complete", message_count=len(messages), history_sha256=digest)
        write_record(handle, record)
    print(f"PASS: health, database, authentication, submission, worker and history; conversation_id={conversation}")


def verify(client, username, record_path):
    record = json.loads(record_path.read_text(encoding="utf-8"))
    require(record["version"] == 1 and record["status"] == "complete", "Receipt is incomplete or unsupported; inspect before retrying")
    require(record["api_url"] == client.base_url and record["username"] == username, "Receipt belongs to a different endpoint or account")
    conversation = str(UUID(record["conversation_id"]))
    job_id = str(UUID(record["job_id"]))
    messages = client.history(conversation)
    require(len(messages) == record["message_count"] and fingerprint(messages) == record["history_sha256"], "Persisted conversation history differs from the saved baseline")
    job = client.call(f"/jobs/{job_id}")
    require(job["status"] == "SUCCEEDED" and job["source_message_id"] == record["message_id"], "Persisted job differs from the baseline")
    print(f"PASS: exact saved conversation history and completed job; conversation_id={conversation}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:18000"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Describe checks without network access (default)")
    mode.add_argument("--write", action="store_true", help="Create one synthetic conversation and job")
    mode.add_argument("--verify-state", type=Path, help="Read-only comparison with a saved receipt")
    parser.add_argument("--record", type=Path, help="New receipt file outside the repository; required with --write")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        base_url = validate_url(args.url)
        require(1 <= args.timeout <= 3600, "Timeout must be between 1 and 3600 seconds")
        require(not args.record or args.write, "--record requires --write")
        if not args.write and not args.verify_state:
            print("PLAN: check health/database and authentication; --write creates one fixed-mode conversation/job, verifies duplicate submission and records a history hash. --verify-state performs GET requests only. No requests sent.")
            return 0
        require(not args.write or args.record is not None, "--write requires a new --record path")
        username = os.environ.get("SMOKE_USERNAME", "")
        password = os.environ.get("SMOKE_PASSWORD", "")
        password_file = os.environ.get("SMOKE_PASSWORD_FILE", "")
        require(not (password and password_file), "Set only one of SMOKE_PASSWORD or SMOKE_PASSWORD_FILE")
        if password_file:
            password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        require(username and password and ":" not in username, "Set SMOKE_USERNAME and a private password or password file")
        client = Client(base_url, username, password)
        health = client.call("/health", authenticated=False)
        require(health.get("status") == "ok" and health.get("database") == "ok", "API/database health check failed")
        require(health.get("reply_mode") == "fixed", "This smoke check requires fixed reply mode")
        require(client.call("/me")["username"] == username, "Authenticated account differs from the requested account")
        if args.verify_state:
            verify(client, username, args.verify_state)
        else:
            exercise(client, username, args.record, args.timeout)
        return 0
    except CheckFailed as error:
        print(f"FAIL: {error}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("FAIL: invalid configuration, receipt, file access or API contract; sensitive details withheld", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
