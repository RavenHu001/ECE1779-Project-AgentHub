"""Exercise the manager rollout with fake services and adapt the real TLS config offline."""

import json
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from test_deployment import BASH, ROOT, bash_path

REPOSITORY = "ghcr.io/team/agenthub/backend"
OLD = REPOSITORY + "@sha256:" + "a" * 64
NEW = REPOSITORY + "@sha256:" + "b" * 64
SCRIPTS = ROOT / "deploy/scripts"

HARNESS = r'''
source "$SCRIPT/rollout.sh"
cd "$STATE_DIR"
release_preflight() { :; }
docker() {
  local svc=${!#}
  case "$1 $2" in
    'service inspect')
      case "$4" in
        *Replicas*) echo "$API_REPLICAS" ;;
        *UpdateStatus*) echo "${UPDATE_STATUS:-completed}" ;;
        *ContainerSpec.Image*) cat "$svc" ;;
        *) exit 90 ;;
      esac ;;
    'service update')
      printf '%s\n' "$*" >> trace
      [[ " $* " == *" --image $AGENTHUB_IMAGE "* && " $* " == *" --update-order start-first "* &&
         " $* " == *" --update-parallelism 1 "* && " $* " == *" --update-failure-action pause "* ]]
      printf '%s\n' "$AGENTHUB_IMAGE" > "$svc" ;;
    *) exit 91 ;;
  esac
}
wait_healthy() {
  [[ $1 == test_db || $(cat "$1") == "$4" ]]
  if [[ $1 == test_api && ${4:-} == "$AGENTHUB_IMAGE" && ${FAULT:-} == timeout ]]; then
    fail 'Simulated health timeout'
  fi
}
run_setup() { echo "setup $*" >> trace; [[ ${FAULT:-} != migration ]]; }
python3() { echo monitor >> trace; [[ ${FAULT:-} != monitor ]]; }
rollout "$ACTION" "$STATE_DIR/release"
'''


class RolloutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        for role in ("api", "worker"):
            (self.directory / ("test_" + role)).write_text(OLD + "\n")
        self.record = self.directory / "release"
        self.trace = self.directory / "trace"
        self.trace.touch()

    def invoke(self, action="deploy", image=NEW, **settings):
        env = dict(os.environ, SCRIPT=bash_path(SCRIPTS), STATE_DIR=bash_path(self.directory),
                   IMAGE_REPOSITORY=REPOSITORY, AGENTHUB_IMAGE=image, ACTION=action, STACK="test",
                   API_REPLICAS="2", WORKER_REPLICAS="2", DB_NODE="manager", API_PUBLISHED_PORT="18000",
                   SCRIPT_DIR=bash_path(SCRIPTS), **settings)
        return subprocess.run([BASH, "-c", HARNESS], env=env, capture_output=True, text=True, timeout=15)

    def test_migration_then_ordered_rollout_and_approved_rollback(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.trace.read_text().splitlines()
        self.assertEqual(len(log), 4)
        self.assertEqual("setup " + NEW, log[0])
        self.assertTrue(log[1].endswith("test_api"))
        self.assertTrue(log[2].endswith("test_worker"))
        self.assertEqual(log[3], "monitor")
        self.assertEqual(self.record.read_text().split(), ["deployed", OLD, NEW])
        self.assertNotEqual(self.invoke("rollback", NEW).returncode, 0)
        self.assertEqual(self.trace.read_text().splitlines(), log)
        result = self.invoke("rollback", OLD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record.read_text().split(), ["reverted", OLD, NEW])
        self.assertEqual(self.trace.read_text().count("setup "), 1)
        self.assertEqual((self.directory / "test_worker").read_text().strip(), OLD)

    def test_interrupted_rollout_blocks_redeploy_and_recovers_only_apps(self):
        self.assertNotEqual(self.invoke(FAULT="timeout").returncode, 0)
        self.assertEqual(self.record.read_text().split(), ["pending", OLD, NEW])
        self.assertEqual((self.directory / "test_worker").read_text().strip(), OLD)
        log = self.trace.read_text()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(self.trace.read_text(), log)
        result = self.invoke("rollback", OLD, UPDATE_STATUS="paused")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" --force ", self.trace.read_text())
        self.assertEqual(self.trace.read_text().count("setup "), 1)

    def test_failed_migration_leaves_applications_on_previous_digest(self):
        self.assertNotEqual(self.invoke(FAULT="migration").returncode, 0)
        self.assertEqual(self.record.read_text().split(), ["migrating", OLD, NEW])
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertNotEqual(self.invoke("rollback", OLD).returncode, 0)
        self.assertEqual(self.trace.read_text().count("setup "), 1)
        self.assertNotIn("service update", self.trace.read_text())

    def test_setup_requires_success_from_the_requested_image(self):
        script = r'''
set -euo pipefail
source "$SCRIPT/common.sh"
docker() {
  case "$*" in
    *Spec.Mode.Replicated*) echo 0 ;;
    *RestartPolicy.Condition*) echo none ;;
    "service ps"*) echo old; if [[ -e "$TRACE" ]]; then echo new; fi ;;
    "service update"*) [[ " $* " == *" --image $CANDIDATE --with-registry-auth "* ]]; echo started > "$TRACE" ;;
    *Status.State*) echo complete ;;
    *Spec.ContainerSpec.Image*) echo "$ACTUAL" ;;
    *ExitCode*) echo 0 ;;
    "service scale"*) echo disarmed >> "$TRACE" ;;
    *) exit 90 ;;
  esac
}
probe_before_deadline() { shift; "$@"; }
wait_stopped() { :; }
run_setup "$CANDIDATE"
'''
        for actual in (OLD, NEW):
            trace = self.directory / actual[-8:]
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=5,
                                    env=dict(os.environ, SCRIPT=bash_path(SCRIPTS), STACK="test", DEPLOY_TIMEOUT="20",
                                             ACTUAL=actual, CANDIDATE=NEW, TRACE=bash_path(trace)))
            self.assertEqual(result.returncode == 0, actual == NEW, result.stderr)
            self.assertEqual("disarmed" in trace.read_text(), actual == NEW)

    def test_failed_final_monitor_preserves_recovery_record(self):
        self.assertNotEqual(self.invoke(FAULT="monitor").returncode, 0)
        self.assertTrue(self.record.read_text().startswith("pending "))

    def test_healthy_old_task_cannot_satisfy_new_digest_gate(self):
        script = r'''
source "$SCRIPT/common.sh"
health_probe() {
  case "$*" in
    *UpdateStatus*) echo completed ;;
    *"service ps"*) echo task-id ;;
    *ContainerSpec.Image*) echo "$ACTUAL" ;;
    *Status.ContainerStatus*) echo 'manager|running|container-id' ;;
    *State.Health*) echo 'running|healthy' ;;
    *) return 90 ;;
  esac
}
service_healthy test_api 1 30 '' "$CANDIDATE"
'''
        for actual in (OLD, NEW):
            result = subprocess.run([BASH, "-c", script], capture_output=True, timeout=5,
                                    env=dict(os.environ, SCRIPT=bash_path(SCRIPTS), MANAGER_ID="manager",
                                             CANDIDATE=NEW, ACTUAL=actual))
            self.assertEqual(result.returncode == 0, actual == NEW)

    def test_rollback_rejects_drift_and_missing_record(self):
        self.assertNotEqual(self.invoke("rollback", OLD).returncode, 0)
        self.record.write_text(f"pending {OLD} {NEW}\n")
        (self.directory / "test_worker").write_text(NEW.replace("b" * 64, "c" * 64))
        self.assertNotEqual(self.invoke("rollback", OLD).returncode, 0)
        self.assertEqual(self.trace.read_text(), "")

    def test_forced_command_rejects_unrecognized_requests_before_reading_config(self):
        for request in ["", "deploy latest", "rollback sha256:123", "deploy " + NEW, "deploy sha256:" + "a"*64 + " extra"]:
            result = subprocess.run([BASH, bash_path(SCRIPTS / "rollout.sh")],
                                    env=dict(os.environ, SSH_ORIGINAL_COMMAND=request),
                                    capture_output=True, text=True, timeout=5)
            self.assertIn("Expected deploy or rollback", result.stderr)
            self.assertNotEqual(result.returncode, 0)


class VolumeTests(unittest.TestCase):
    def check(self, uuid="expected", options="rw,relatime", percent=79):
        cmd = r'''
findmnt() { if [[ $3 == UUID ]]; then echo "$ACTUAL_UUID"; else echo "$OPTIONS"; fi; }
df() { printf 'header\n/dev/test 100 79 21 %s%% /mnt/db\n' "$PERCENT"; }
source "$SCRIPT/check_volume.sh"
'''
        return subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=5, env=dict(
            os.environ, SCRIPT=bash_path(SCRIPTS), POSTGRES_VOLUME_MOUNT="/mnt/db",
            POSTGRES_VOLUME_UUID="expected", ACTUAL_UUID=uuid, OPTIONS=options, PERCENT=str(percent)))

    def test_capacity_mount_identity_and_read_only_alerts(self):
        self.assertEqual(self.check().returncode, 0)
        for options in [dict(uuid=""), dict(uuid="wrong"), dict(options="ro"), dict(percent=80)]:
            with self.subTest(options=options):
                self.assertNotEqual(self.check(**options).returncode, 0)


class WorkflowTests(unittest.TestCase):
    def test_approval_command_requires_independent_production_reviewer(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf8")
        step = workflow.split("      - name: Verify independent production approval\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        fake = r'''
gh() {
  [[ "$*" == "api repos/team/agenthub/actions/runs/9/approvals" ]] || return 2
  printf '%s\n' "$REVIEWS"
  return "$API_STATUS"
}
python3() { "$TEST_PYTHON" "$@"; }
'''
        valid = {"state": "approved", "user": {"id": 8, "type": "User"}, "environments": [{"name": "production"}]}
        for label, reviews, attempt, status, accepted in (
            ("approved", [valid], "1", "0", True),
            ("no reviews", [], "1", "0", False),
            ("self approval", [dict(valid, user={"id": 7, "type": "User"})], "1", "0", False),
            ("missing reviewer", [dict(valid, user={})], "1", "0", False),
            ("bot reviewer", [dict(valid, user={"id": 8, "type": "Bot"})], "1", "0", False),
            ("wrong environment", [dict(valid, environments=[{"name": "staging"}])], "1", "0", False),
            ("rejected", [dict(valid, state="rejected")], "1", "0", False),
            ("rerun", [valid], "2", "0", False),
            ("API failure", [valid], "1", "1", False),
        ):
            with self.subTest(label=label):
                env = dict(os.environ, GITHUB_RUN_ATTEMPT=attempt, GITHUB_REPOSITORY="team/agenthub",
                           GITHUB_RUN_ID="9", GITHUB_ACTOR_ID="7", TEST_PYTHON=sys.executable,
                           REVIEWS=json.dumps(reviews), API_STATUS=status)
                result = subprocess.run([BASH, "-c", fake + script], env=env, capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_ssh_request_uses_only_the_digest_approved_for_the_operation(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf8")
        job = workflow.split("\n  deliver:\n", 1)[1]
        for gate in ["environment: production", "github.ref == 'refs/heads/main'",
                     "needs.publish.result == 'success'", "StrictHostKeyChecking=yes"]:
            self.assertIn(gate, job)
        step = job.split("      - name: Request the approved manager operation\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        fake = 'ssh() { printf "%s\\n" "${!#}"; }\n'
        with tempfile.TemporaryDirectory() as tmp:
            for action, published, rollback, expected in [
                ("deploy", NEW, OLD, NEW), ("rollback", NEW, OLD, OLD),
                ("deploy", "", OLD, ""), ("rollback", NEW, "", ""), ("other", NEW, OLD, ""),
            ]:
                env = dict(os.environ, OPERATION=action, TARGET="delivery@manager", RUNNER_TEMP=bash_path(tmp),
                           PUBLISHED_DIGEST=published.partition("@")[2], ROLLBACK_DIGEST=rollback.partition("@")[2],
                           HOST_KEY="fixture", IDENTITY="fixture")
                result = subprocess.run([BASH, "-c", fake + script], env=env, capture_output=True, text=True, timeout=5)
                with self.subTest(action=action, expected=expected):
                    if expected:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), action + " " + expected.split("@")[1])
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                    self.assertEqual(list(Path(tmp).iterdir()), [])


@unittest.skipUnless(shutil.which("caddy"), "Install Caddy to run configuration adaptation")
class HttpsTests(unittest.TestCase):
    def adapt(self, domain, port):
        return subprocess.run(["caddy", "adapt", "--config", str(ROOT / "deploy/caddy/Caddyfile")],
                              env=dict(os.environ, AGENTHUB_DOMAIN=domain, API_PUBLISHED_PORT=port),
                              capture_output=True, text=True, timeout=10)

    def test_public_host_automatic_https_and_unbuffered_sse(self):
        result = self.adapt("agenthub.example.org", "18001")
        self.assertEqual(result.returncode, 0, result.stderr)
        server, = json.loads(result.stdout)["apps"]["http"]["servers"].values()
        self.assertEqual(server["listen"], [":443"])
        self.assertNotIn("automatic_https", server)
        route, = server["routes"]
        self.assertEqual(route["match"], [{"host": ["agenthub.example.org"]}])
        handlers = route["handle"][0]["routes"][0]["handle"]
        proxy, = (h for h in handlers if h["handler"] == "reverse_proxy")
        self.assertEqual(proxy["upstreams"], [{"dial": "127.0.0.1:18001"}])
        self.assertNotIn("response_buffers", proxy)
        self.assertIn("Strict-Transport-Security", str(handlers))

    def test_empty_or_plain_http_site_fails_closed(self):
        for domain in ("", "http://agenthub.example.org"):
            with self.subTest(domain=domain):
                self.assertNotEqual(self.adapt(domain, "18000").returncode, 0)

    def test_redirect_and_event_arrival_before_upstream_closes(self):
        release = threading.Event()

        class Stream(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: ready\n\n")
                self.wfile.flush()
                release.wait(8)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Stream) as upstream:
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            with socket.socket() as plain, socket.socket() as secure:
                for listener in (plain, secure):
                    listener.bind(("127.0.0.1", 0))
                http_port, https_port = plain.getsockname()[1], secure.getsockname()[1]
            directory = Path(tmp)
            options = (
                "{\n admin off\n persist_config off\n local_certs\n skip_install_trust\n"
                f' default_bind 127.0.0.1\n storage file_system "{directory.as_posix()}/pki"\n'
                f" http_port {http_port}\n https_port {https_port}\n}}\n"
            )
            config = directory / "Caddyfile"
            config.write_text(options + (ROOT / "deploy/caddy/Caddyfile").read_text(), encoding="utf8")
            env = dict(os.environ, AGENTHUB_DOMAIN="localhost", API_PUBLISHED_PORT=str(upstream.server_port),
                       APPDATA=tmp, XDG_DATA_HOME=tmp, XDG_CONFIG_HOME=tmp)
            proc = subprocess.Popen(["caddy", "run", "--config", str(config)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                ca = directory / "pki/pki/authorities/local/root.crt"
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    self.assertIsNone(proc.poll(), "Caddy exited before serving the test site")
                    if ca.exists():
                        try:
                            context = ssl.create_default_context(cafile=str(ca))
                            with socket.create_connection(("127.0.0.1", https_port), timeout=0.5) as sock:
                                with context.wrap_socket(sock, server_hostname="localhost"):
                                    break
                        except OSError:
                            pass
                    time.sleep(0.05)
                conn = HTTPConnection("localhost", http_port, timeout=3)
                try:
                    conn.request("GET", "/events")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 308)
                    self.assertEqual(response.getheader("Location"), "https://localhost/events")
                finally:
                    conn.close()
                conn = HTTPSConnection("localhost", https_port, timeout=3, context=ssl.create_default_context(cafile=str(ca)))
                try:
                    conn.request("GET", "/events")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Strict-Transport-Security"), "max-age=31536000")
                    self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
                    self.assertEqual(response.readline(), b"data: ready\n")
                    self.assertEqual(response.readline(), b"\n")
                finally:
                    conn.close()
            finally:
                release.set()
                proc.terminate()
                proc.wait(timeout=10)
                upstream.shutdown()
