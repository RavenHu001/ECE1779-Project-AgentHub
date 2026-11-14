"""Offline orchestration tests: every Docker command goes to a strict fake CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


def bash_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


def fake_docker(args):
    """Fail on unknown commands rather than ever falling back to a real daemon."""
    sys.stdout.reconfigure(newline="\n")
    state_path = Path(os.environ["FAKE_STATE"])
    state = json.loads(state_path.read_text())
    fault = state.get("fault", "")
    state["calls"].append({"args": args, "api": os.environ.get("API_REPLICAS"), "worker": os.environ.get("WORKER_REPLICAS")})
    output = ""
    code = 0
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    stack = os.environ["STACK"]
    if args[:2] == ["stack", "config"]:
        code = int(fault == "invalid-config")
    elif args[:2] == ["context", "inspect"]:
        output = "unix:///var/run/docker.sock"
    elif args[0] == "info":
        output = os.environ.get("FAKE_NODE", "manager") if "NodeID" in fmt else "Ubuntu" if "OperatingSystem" in fmt else "linux active true"
    elif args[:2] == ["node", "ls"]:
        output = "manager\nworker" if os.environ.get("TWO_NODES") else "manager"
    elif args[:2] == ["node", "inspect"]:
        if "Status.State" in fmt:
            output = "ready active linux"
        elif "Architecture" in fmt:
            output = "amd64"
        elif '"agenthub.node"' in fmt:
            output = args[-1]
        elif '"agenthub.db"' in fmt:
            output = "true" if fault == "duplicate-label" or (fault != "missing-label" and args[-1] == "manager") else "false"
        elif "Spec.Role" in fmt:
            output = "worker"
        else:
            code = 90
    elif args[:2] == ["secret", "inspect"]:
        code = int(fault == "missing-secret")
    elif args[:2] == ["image", "inspect"]:
        output = json.dumps([
            {"Os": "linux", "Architecture": "amd64", "Config": {"Env": []}},
            {"Os": "linux", "Architecture": "amd64", "Config": {"Env": ["PG_MAJOR=16" if fault == "wrong-image" else "PG_MAJOR=17"]}},
        ])
    elif args[:2] == ["manifest", "inspect"]:
        code = int(fault == "registry-unavailable")
    elif args[:2] == ["service", "ls"]:
        if fault == "armed-setup":
            output = stack + "_setup"
        elif fault in ("changed-database", "existing-database"):
            output = stack + "_db"
    elif args[:2] == ["service", "inspect"]:
        if "json .Spec" in fmt:
            output = json.dumps({
                "Name": stack + "_db", "Labels": {"com.docker.stack.namespace": stack},
                "TaskTemplate": {"ContainerSpec": {
                    "Image": "postgres:16" if fault == "changed-database" else os.environ["POSTGRES_IMAGE"],
                    "Env": ["POSTGRES_DB=" + os.environ["POSTGRES_DB"], "POSTGRES_USER=" + os.environ["POSTGRES_USER"]],
                    "Mounts": [{"Type": "bind", "Source": os.environ["POSTGRES_DATA_PATH"], "Target": "/var/lib/postgresql/data"}],
                    "Secrets": [{"SecretName": os.environ["DB_PASSWORD_SECRET"]}],
                }}, "Mode": {"Replicated": {"Replicas": 1}},
            })
        elif "Replicas" in fmt:
            output = "1" if fault == "armed-setup" or state.get("setup") else "0"
        elif "RestartPolicy" in fmt:
            output = "none"
        elif "UpdateStatus" in fmt:
            output = "paused" if fault == "paused-db" else "completed"
        else:
            code = 90
    elif args[:2] == ["service", "ps"]:
        service = args[-1].removeprefix(stack + "_")
        if service == "setup":
            output = "old-setup"
            if state.get("launched"):
                output += "\nnew-setup"
                if fault == "duplicate-setup":
                    output += "\nextra-setup"
        elif service == "db":
            output = "db-1"
        elif service in ("api", "worker"):
            output = f"{service}-1\n{service}-2" if state.get("apps") else ""
        else:
            code = 90
    elif args[:2] == ["stack", "deploy"]:
        state["deployed"] = True
    elif args[:2] == ["service", "update"]:
        state.update(setup=True, launched=True)
    elif args[:2] == ["service", "scale"]:
        if stack + "_setup=0" in args:
            state["setup"] = False
        else:
            state["apps"] = True
    elif args[:2] == ["ps", "-aq"]:
        output = "unrelated" if fault == "container-conflict" else ""
    elif args == ["inspect", "unrelated"]:
        output = json.dumps([{"Mounts": [{"Type": "bind", "Source": os.environ["POSTGRES_DATA_PATH"]}]}])
    elif args[0] == "inspect" and "--type" in args:
        ids = args[args.index("--format") + 2:]
        values = []
        for task in ids:
            if "ContainerStatus.ExitCode" in fmt:
                value = "7" if fault == "nonzero-setup" else "0"
            elif "|" in fmt:
                value = f"manager|running|container-{task}"
            elif fmt == "{{.NodeID}}":
                value = "manager"
            else:
                value = "complete"
                if task == "new-setup":
                    value = {"failed-setup": "failed", "timeout-setup": "running"}.get(fault, "complete")
            values.append(value)
        output = "\n".join(values)
    elif args[0] == "inspect" and "State.Health" in fmt:
        output = "unhealthy" if fault == "unhealthy-db" else "healthy"
    else:
        code = 90
    state_path.write_text(json.dumps(state))
    if output:
        print(output)
    if code == 90:
        print("Unsupported fake Docker call", file=sys.stderr)
    return code


@unittest.skipUnless(BASH, "Bash is required")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.data = self.directory / "postgres"
        self.data.mkdir()
        (self.data / "PG_VERSION").write_text("17\n")
        self.state_path = self.directory / "state.json"
        self.state_path.write_text(json.dumps({"calls": []}))
        for name, body in {
            "docker": 'exec "$TEST_PYTHON" "$TEST_DRIVER" fake-docker "$@"',
            "python3": 'exec "$TEST_PYTHON" "$@"',
            "uname": "printf 'Linux\\n'",
            "flock": 'test "${LOCK_BUSY:-0}" = 0',
            "ssh": 'export FAKE_NODE=${FAKE_REMOTE_ID:-worker}; exec bash -c "${@: -1}"',
            "mountpoint": 'test "${MOUNT_MISSING:-0}" = 0',
            "findmnt": 'case "$3" in UUID) printf "%s\\n" "${ACTUAL_UUID:-test-volume-uuid}";; TARGET) printf "%s\\n" "$POSTGRES_VOLUME_MOUNT";; OPTIONS) printf "%s\\n" "${MOUNT_OPTIONS:-rw,relatime}";; *) exit 90;; esac',
        }.items():
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + body + "\n", newline="\n")
            path.chmod(0o755)
        self.env = os.environ.copy()
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "BASH_ENV", "ENV"):
            self.env.pop(name, None)
        self.env.update({
            "TEST_BIN": bash_path(self.bin), "TEST_PYTHON": Path(sys.executable).as_posix(),
            "TEST_DRIVER": Path(__file__).resolve().as_posix(), "FAKE_STATE": str(self.state_path),
            "STACK": "offline-test", "AGENTHUB_IMAGE": "agenthub:offline-v1", "POSTGRES_IMAGE": "postgres:17-bookworm",
            "POSTGRES_DATA_PATH": bash_path(self.data), "POSTGRES_DB": "agenthub", "POSTGRES_USER": "agenthub",
            "BOOTSTRAP_USERNAME": "developer", "DB_PASSWORD_SECRET": "offline-db", "BOOTSTRAP_PASSWORD_SECRET": "offline-bootstrap",
            "IMAGE_MODE": "local", "STORAGE_MODE": "directory", "API_REPLICAS": "2", "WORKER_REPLICAS": "2",
            "API_PUBLISHED_PORT": "18099", "DEPLOY_TIMEOUT": "2", "DEPLOY_LOCK_PATH": bash_path(self.directory / "lock"),
            "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*",
        })

    def state(self):
        return json.loads(self.state_path.read_text())

    def run_script(self, script="deploy.sh", mode="--apply", fault=""):
        state = self.state()
        state["fault"] = fault
        self.state_path.write_text(json.dumps(state))
        command = 'export PATH="$TEST_BIN:$PATH"; exec bash "$1" "$2"'
        return subprocess.run([BASH, "-c", command, "test", bash_path(ROOT / "deploy/scripts" / script), mode], env=self.env, capture_output=True, text=True, timeout=40)

    def mutations(self):
        return [call for call in self.state()["calls"] if call["args"][:2] in (["stack", "deploy"], ["service", "update"], ["service", "scale"])]

    def test_plan_does_not_contact_daemon(self):
        result = self.run_script(mode="--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["args"][:2] for call in self.state()["calls"]], [["stack", "config"]])

    def test_missing_variable_fails_before_docker(self):
        self.env.pop("DB_PASSWORD_SECRET")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["calls"], [])

    def test_registry_port_is_not_mistaken_for_an_image_tag(self):
        self.env["AGENTHUB_IMAGE"] = "registry.example:5000/agenthub"
        result = self.run_script(mode="--plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["calls"], [])

    def test_preflight_failures_never_mutate(self):
        for fault in ("invalid-config", "missing-label", "missing-secret", "wrong-image", "armed-setup", "changed-database", "container-conflict"):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({"calls": []}))
                result = self.run_script(fault=fault)
                self.assertNotEqual(result.returncode, 0, fault)
                self.assertEqual(self.mutations(), [], result.stdout + result.stderr)

    def test_wrong_postgres_directory_fails_before_mutation(self):
        (self.data / "PG_VERSION").write_text("16\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("storage check failed", result.stderr)
        self.assertEqual(self.mutations(), [])

    def test_busy_lock_prevents_online_checks(self):
        self.env["LOCK_BUSY"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manager lock", result.stderr)
        self.assertEqual(len(self.state()["calls"]), 1)

    def test_success_stages_once_and_ignores_historical_success(self):
        result = self.run_script(fault="existing-database")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mutations = self.mutations()
        self.assertEqual(len(mutations), 4)
        self.assertEqual(mutations[0]["args"][:2], ["stack", "deploy"])
        self.assertEqual((mutations[0]["api"], mutations[0]["worker"]), ("0", "0"))
        self.assertEqual(mutations[1]["args"][:2], ["service", "update"])
        self.assertIn("offline-test_setup=0", mutations[2]["args"])
        self.assertIn("offline-test_api=2", mutations[3]["args"])
        self.assertIn("offline-test_worker=2", mutations[3]["args"])
        self.assertFalse(self.state()["setup"])
        self.assertIn("exit code 0", result.stdout)

    def test_failed_setup_never_starts_applications_or_retries(self):
        for fault in ("failed-setup", "nonzero-setup", "duplicate-setup", "timeout-setup"):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({"calls": []}))
                result = self.run_script(fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.state().get("apps", False))
                self.assertTrue(self.state()["setup"])
                self.assertEqual(len(self.mutations()), 2)

    def test_unhealthy_or_paused_database_never_runs_setup(self):
        for fault in ("unhealthy-db", "paused-db"):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({"calls": []}))
                result = self.run_script(fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.state().get("launched", False))
                self.assertEqual(len(self.mutations()), 1)

    def two_nodes(self):
        self.env.update({
            "TWO_NODES": "1", "WORKER_SSH": "fake-worker", "IMAGE_MODE": "registry", "STORAGE_MODE": "volume",
            "POSTGRES_VOLUME_MOUNT": bash_path(self.directory), "POSTGRES_VOLUME_UUID": "test-volume-uuid",
            "AGENTHUB_IMAGE": "ghcr.io/example/agenthub/backend@sha256:" + "a" * 64,
            "POSTGRES_IMAGE": "docker.io/library/postgres@sha256:" + "b" * 64,
        })

    def test_two_node_preflight_checks_remote_images_and_registry(self):
        self.two_nodes()
        result = self.run_script(script="preflight.sh", mode="--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [c["args"][:2] for c in self.state()["calls"]]
        self.assertEqual(calls.count(["manifest", "inspect"]), 2)
        self.assertEqual(calls.count(["image", "inspect"]), 2)
        self.assertEqual(self.mutations(), [])

    def test_volume_registry_and_remote_identity_fail_closed(self):
        self.two_nodes()
        for fault, changes in (
            ("duplicate-label", {}), ("registry-unavailable", {}),
            ("", {"FAKE_REMOTE_ID": "wrong-node"}), ("", {"MOUNT_MISSING": "1"}),
            ("", {"ACTUAL_UUID": "different-volume"}), ("", {"MOUNT_OPTIONS": "ro,relatime"}),
        ):
            with self.subTest(fault=fault, changes=changes):
                self.state_path.write_text(json.dumps({"calls": []}))
                self.env.update(changes)
                try:
                    result = self.run_script(fault=fault)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.mutations(), [])
                finally:
                    for key in changes:
                        self.env.pop(key)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fake-docker":
        sys.exit(fake_docker(sys.argv[2:]))
    unittest.main()
