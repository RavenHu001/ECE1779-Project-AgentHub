"""Contract tests against an ephemeral mock HTTP server, never a deployment."""

from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

spec = importlib.util.spec_from_file_location("delivery_smoke", Path(__file__).resolve().parents[1] / "scripts/smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = Path(self.temp.name) / "receipt.json"
        self.conversation, self.message, self.job = (str(uuid4()) for _ in range(3))
        self.requests = []
        self.fault = ""
        self.messages = [
            {"id": self.message, "conversation_id": self.conversation, "seq": 1, "role": "user", "content": "Hello", "reply_to_message_id": None, "created_at": "2026-01-01T00:00:00Z"},
            {"id": str(uuid4()), "conversation_id": self.conversation, "seq": 2, "role": "assistant", "content": "Message received.", "reply_to_message_id": self.message, "created_at": "2026-01-01T00:00:01Z"},
        ]
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_call()

            def do_POST(self):
                self.handle_call()

            def handle_call(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))) if "Content-Length" in self.headers else None
                test.requests.append((self.command, self.path, body))
                status = 200
                if test.fault == "redirect":
                    self.send_response(302)
                    self.send_header("Location", test.url + "/redirected")
                    self.end_headers()
                    return
                if self.path == "/health":
                    payload = {"status": "ok", "database": "bad" if test.fault == "database" else "ok", "reply_mode": "model" if test.fault == "model" else "fixed"}
                elif test.fault == "auth" or self.headers.get("Authorization") != "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ=":
                    status, payload = 401, {"detail": "test-password must never appear in error output"}
                elif self.path == "/me":
                    payload = {"username": "test-user"}
                elif self.path == "/conversations" and self.command == "POST":
                    status, payload = 201, {"id": test.conversation}
                elif self.path.startswith("/conversations?"):
                    payload = [{"id": test.conversation}]
                elif self.path.startswith(f"/conversations/{test.conversation}/messages"):
                    if self.command == "POST":
                        status, payload = 202, {"message_id": test.message, "job_id": test.job}
                        if test.fault == "duplicate" and len([r for r in test.requests if r[0] == "POST"]) == 3:
                            payload["job_id"] = str(uuid4())
                    else:
                        payload = test.messages
                elif self.path == f"/jobs/{test.job}":
                    payload = {"status": "FAILED" if test.fault == "job" else "SUCCEEDED", "source_message_id": test.message}
                else:
                    status, payload = 404, {}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_smoke(self, *args):
        output = io.StringIO()
        with patch.dict(os.environ, {"SMOKE_USERNAME": "test-user", "SMOKE_PASSWORD": "test-password", "SMOKE_PASSWORD_FILE": ""}), redirect_stdout(output), redirect_stderr(output):
            result = smoke.main(["--url", self.url, *args])
        self.assertNotIn("test-password", output.getvalue())
        return result, output.getvalue()

    def test_default_plan_never_contacts_server(self):
        self.assertEqual(self.run_smoke()[0], 0)
        self.assertEqual(self.requests, [])

    def test_write_then_read_only_exact_history(self):
        self.assertEqual(self.run_smoke("--write", "--record", str(self.record))[0], 0)
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[1][2], posts[2][2])
        saved = json.loads(self.record.read_text())
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["message_count"], 2)
        self.assertNotIn("test-password", self.record.read_text())
        self.assertNotIn("Message received.", self.record.read_text())
        self.requests.clear()
        self.assertEqual(self.run_smoke("--verify-state", str(self.record))[0], 0)
        self.assertTrue(all(r[0] == "GET" for r in self.requests))
        self.messages[1]["content"] = "Changed after restart"
        self.assertEqual(self.run_smoke("--verify-state", str(self.record))[0], 1)

    def test_existing_receipt_prevents_writes(self):
        self.record.write_text("retain this receipt")
        self.assertEqual(self.run_smoke("--write", "--record", str(self.record))[0], 1)
        self.assertFalse(any(r[0] == "POST" for r in self.requests))
        self.assertEqual(self.record.read_text(), "retain this receipt")

    def test_health_auth_and_mode_fail_before_writes(self):
        for fault in ("database", "model", "auth"):
            with self.subTest(fault=fault):
                self.fault = fault
                self.requests.clear()
                self.assertEqual(self.run_smoke("--write", "--record", str(self.record))[0], 1)
                self.assertFalse(any(r[0] == "POST" for r in self.requests))

    def test_duplicate_and_worker_failure_leave_incomplete_receipt(self):
        for fault in ("duplicate", "job"):
            with self.subTest(fault=fault):
                self.fault = fault
                self.requests.clear()
                path = self.record.with_name(fault + ".json")
                self.assertEqual(self.run_smoke("--write", "--record", str(path))[0], 1)
                self.assertEqual(json.loads(path.read_text())["status"], "pending")

    def test_redirect_is_not_followed(self):
        self.fault = "redirect"
        self.assertEqual(self.run_smoke("--write", "--record", str(self.record))[0], 1)
        self.assertEqual(len(self.requests), 1)

    def test_unsafe_urls_are_rejected(self):
        for url in ("http://public.example", "http://user:password@localhost", "http://localhost/path", "http://localhost?token=value"):
            with self.subTest(url=url), self.assertRaises(smoke.CheckFailed):
                smoke.validate_url(url)


if __name__ == "__main__":
    unittest.main()
