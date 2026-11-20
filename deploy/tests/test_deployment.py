# 离线验证部署编排：所有 Docker 命令均由严格校验的模拟 CLI 处理。
"""Offline orchestration tests: every Docker command goes to a strict fake CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def resolve_bash_path(env=None, platform=None, which=None):
    env = os.environ if env is None else env
    platform = os.name if platform is None else platform
    configured = env.get("GIT_BASH_PATH")
    if configured:
        bash_path = Path(configured)

        if not bash_path.is_file():
            raise RuntimeError(
                f"GIT_BASH_PATH does not point to a file: {configured}"
            )

        try:
            result = subprocess.run(
                [str(bash_path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"GIT_BASH_PATH is not a working Bash executable: {configured}"
            ) from exc

        if "GNU bash" not in result.stdout:
            raise RuntimeError(
                f"GIT_BASH_PATH is not a GNU Bash executable: {configured}"
            )

        return str(bash_path)
    which = shutil.which if which is None else which
    if platform != "nt":
        return which("bash")

    git_executable = which("git")
    if git_executable:
        git_directory = Path(git_executable).resolve().parent
        roots = [git_directory]
        if git_directory.name.lower() in {"bin", "cmd"}:
            roots.insert(0, git_directory.parent)
        for root in roots:
            for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)

    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = env.get(variable)
        if program_files:
            candidate = Path(program_files) / "Git" / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)

    raise RuntimeError("Git Bash was not found. Set GIT_BASH_PATH to the full path of bash.exe.")


def bash_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


def fake_docker(args):
    # 遇到未识别的命令时直接失败，避免调用真实 Docker daemon。
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
                    "Image": "postgres:16" if fault == "changed-database" else state.get("db_image", os.environ["POSTGRES_IMAGE"]),
                    "Env": ["POSTGRES_DB=" + os.environ["POSTGRES_DB"], "POSTGRES_USER=" + os.environ["POSTGRES_USER"]],
                    "Mounts": [{"Type": "bind", "Source": os.environ["POSTGRES_DATA_PATH"], "Target": "/var/lib/postgresql/data"}],
                    "Secrets": [{"SecretName": os.environ["DB_PASSWORD_SECRET"]}],
                }}, "Mode": {"Replicated": {"Replicas": 1}},
            })
        elif "Replicas" in fmt:
            output = "1" if fault == "armed-setup" or state.get("setup") else "0"
            if fault == "failed-replica-inspection":
                code = 1
        elif "RestartPolicy" in fmt:
            output = "on-failure" if fault == "retry-enabled" else "none"
            if fault == "failed-restart-inspection":
                code = 1
        elif "UpdateStatus" in fmt:
            output = state.get("update", "paused" if fault == "paused-db" else "completed")
        else:
            code = 90
    elif args[:2] == ["service", "ps"]:
        service = args[-1].removeprefix(stack + "_")
        if service == "setup":
            output = "old-setup"
            if state.get("launched") and fault != "historical-only":
                output += "\nnew-setup"
                if fault == "duplicate-setup" or (fault == "late-duplicate" and not state["setup"]):
                    output += "\nextra-setup"
        elif service == "db":
            output = "db-1"
        elif service in ("api", "worker"):
            output = state.get("task_list", f"{service}-1\n{service}-2") if state.get("apps") else ""
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
                node = "worker" if os.environ.get("TWO_NODES") and task.endswith("-1") and task != "db-1" else "manager"
                value = f"{node}|running|container-{task}"
            elif fmt == "{{.NodeID}}":
                value = "manager"
            else:
                value = "complete"
                if task == "new-setup":
                    value = {"failed-setup": "failed", "timeout-setup": "running"}.get(fault, "complete")
            values.append(value)
        output = "\n".join(values)
    elif args[0] == "inspect" and "State.Health" in fmt:
        health = "unhealthy" if fault in ("unhealthy-db", "unhealthy-worker") else "healthy"
        output = f"running|{health}" if ".State.Status" in fmt else health
        if os.environ.get("TWO_NODES") and args[-1].startswith(("container-api-", "container-worker-")):
            expected_node = "worker" if args[-1].endswith("-1") else "manager"
            if os.environ.get("FAKE_NODE", "manager") != expected_node:
                code = 90
    else:
        code = 90
    # A failed query may still produce stdout. A retry must discard that result.
    # 查询失败时仍可能写入标准输出；重试时必须丢弃该结果。
    if args[:2] + [args[-1]] == state.get("fail_query"):
        if state.get("permanent") or not state.get("query_failed"):
            state["query_failed"] = True
            code = 1
    state_path.write_text(json.dumps(state))
    if output:
        print(output)
    if code == 90:
        print("Unsupported fake Docker call", file=sys.stderr)
    return code


# Fake CLI children inherit a rewritten PATH and need no Bash discovery or validation.
# 模拟 CLI 子进程继承已改写的 PATH，无需查找或验证 Bash。
if __name__ == "__main__" and sys.argv[1:2] == ["fake-docker"]:
    sys.exit(fake_docker(sys.argv[2:]))

BASH = resolve_bash_path()


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
        # Allow subprocess startup in orchestration tests; deadline tests set shorter budgets.
        # 编排测试为子进程启动留出余量；截止时间专项测试单独设置短预算。
        self.env.update({
            "TEST_BIN": bash_path(self.bin), "TEST_PYTHON": Path(sys.executable).as_posix(),
            "TEST_DRIVER": Path(__file__).resolve().as_posix(), "FAKE_STATE": str(self.state_path),
            "STACK": "offline-test", "AGENTHUB_IMAGE": "agenthub:offline-v1", "POSTGRES_IMAGE": "postgres:17-bookworm",
            "POSTGRES_DATA_PATH": bash_path(self.data), "POSTGRES_DB": "agenthub", "POSTGRES_USER": "agenthub",
            "BOOTSTRAP_USERNAME": "developer", "DB_PASSWORD_SECRET": "offline-db", "BOOTSTRAP_PASSWORD_SECRET": "offline-bootstrap",
            "IMAGE_MODE": "local", "STORAGE_MODE": "directory", "API_REPLICAS": "2", "WORKER_REPLICAS": "2",
            "API_PUBLISHED_PORT": "18099", "DEPLOY_TIMEOUT": "10", "DEPLOY_LOCK_PATH": bash_path(self.directory / "lock"),
            "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*",
        })

    def state(self):
        return json.loads(self.state_path.read_text())

    def run_script(self, script="deploy.sh", mode="--apply", fault="", timeout=40):
        state = self.state()
        state["fault"] = fault
        self.state_path.write_text(json.dumps(state))
        command = 'export PATH="$TEST_BIN:$PATH"; exec bash "$1" "$2"'
        return subprocess.run([BASH, "-c", command, "test", bash_path(ROOT / "deploy/scripts" / script), mode], env=self.env, capture_output=True, text=True, timeout=timeout)

    def run_health_probe(self, delay, health="healthy", timeout=3):
        docker = self.bin / "docker"
        self.probe_trace = self.directory / "probe-trace.log"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$PROBE_TRACE"; '
            'if [[ "$1 $2" == "service inspect" ]]; then printf "completed\\n"; '
            'elif [[ "$1 $2" == "service ps" ]]; then printf "task-1\\n"; '
            'elif [[ "$1" == inspect && "$*" == *"type task"* ]]; then printf "manager|running|container-1\\n"; '
            f'elif [[ "$1" == inspect && "$*" == *"State.Health"* ]]; then sleep {delay}; printf "health-result:{health}\\n" >> "$PROBE_TRACE"; printf "running|{health}\\n"; '
            'else exit 90; fi\n',
            newline="\n",
        )
        docker.chmod(0o755)
        command = 'export PATH="$TEST_BIN:$PATH" MANAGER_ID=manager DEPLOY_TIMEOUT="$PROBE_TIMEOUT"; source "$COMMON_SH"; wait_healthy api 1'
        env = self.env | {
            "PROBE_TIMEOUT": str(timeout),
            "COMMON_SH": bash_path(ROOT / "deploy/scripts/common.sh"),
            "PROBE_TRACE": str(self.probe_trace),
        }
        return subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=12)

    def run_simulated_health_probe(self, delay, health="healthy", timeout=30):
        self.probe_trace = self.directory / "probe-trace.log"
        command = r'''
export MANAGER_ID=manager DEPLOY_TIMEOUT="$PROBE_TIMEOUT"
source "$COMMON_SH"
timeout() {
    while [[ $1 == --* ]]; do shift; done
    shift
    "$@"
}
docker() {
    printf '%s\n' "$*" >> "$PROBE_TRACE"
    if [[ "$1 $2" == "service inspect" ]]; then
        printf 'completed\n'
    elif [[ "$1 $2" == "service ps" ]]; then
        printf 'task-1\n'
    elif [[ "$1" == inspect && "$*" == *"type task"* ]]; then
        printf 'manager|running|container-1\n'
    elif [[ "$1" == inspect && "$*" == *"State.Health"* ]]; then
        local health_probe=1
        command sleep "$PROBE_DELAY"
        printf 'health-result:%s\n' "$PROBE_HEALTH" >> "$PROBE_TRACE"
        printf 'running|%s\n' "$PROBE_HEALTH"
    else
        return 90
    fi
}
sleep() {
    if [[ ${health_probe:-0} == 1 ]]; then
        command sleep "$@"
    else
        SECONDS=$deadline
    fi
}
wait_healthy api 1
'''
        env = self.env | {
            "PROBE_DELAY": str(delay),
            "PROBE_HEALTH": health,
            "PROBE_TIMEOUT": str(timeout),
            "COMMON_SH": bash_path(ROOT / "deploy/scripts/common.sh"),
            "PROBE_TRACE": str(self.probe_trace),
        }
        return subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=12)

    def run_stop_probe(self, state="shutdown", slow="", simulated=False):
        # Use a shell fake so timeout covers the actual child process, including sleep.
        # 使用 shell 模拟命令，让 timeout 覆盖真实子进程及其 sleep。
        docker = self.bin / "docker"
        docker.write_text(r'''#!/usr/bin/env bash
if [[ "$1 $2" == "service ps" ]]; then
  if [[ $SLOW_QUERY == list ]]; then sleep 30; fi
  if [[ $TASK_STATE != empty ]]; then printf 'task-1\n'; fi
elif [[ "$1" == inspect ]]; then
  if [[ $SLOW_QUERY == inspect ]]; then sleep 30; fi
  printf '%s\n' "$TASK_STATE"
else
  exit 90
fi
''', newline="\n")
        docker.chmod(0o755)
        command = r'''
export PATH="$TEST_BIN:$PATH" DEPLOY_TIMEOUT=2
source "$COMMON_SH"
'''
        if simulated:
            # A late successful query must fail even if the timeout utility returns success.
            # 即使 timeout 工具返回成功，迟到的查询结果也必须被拒绝。
            command += r'''
probe_before_deadline() { shift; "$@"; }
docker() { command sleep 2; printf 'shutdown\n'; }
DEPLOY_TIMEOUT=1
'''
        command += 'wait_stopped api\nprintf "stopped-before-deadline\\n"\n'
        env = self.env | {
            "COMMON_SH": bash_path(ROOT / "deploy/scripts/common.sh"),
            "TASK_STATE": state, "SLOW_QUERY": slow,
        }
        return subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=9)

    def test_wait_stopped_accepts_empty_and_terminal_tasks(self):
        for state in ("empty", "complete", "shutdown", "failed", "rejected"):
            with self.subTest(state=state):
                result = self.run_stop_probe(state=state)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_stopped_rejects_running_and_orphaned_tasks(self):
        for state in ("running", "orphaned"):
            with self.subTest(state=state):
                result = self.run_stop_probe(state=state)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Tasks did not stop", result.stderr)

    def test_wait_stopped_rejects_success_after_deadline(self):
        result = self.run_stop_probe(simulated=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tasks did not stop", result.stderr)
        self.assertNotIn("stopped-before-deadline", result.stdout)

    def test_wait_stopped_bounds_both_docker_queries(self):
        for query in ("list", "inspect"):
            with self.subTest(query=query):
                result = self.run_stop_probe(slow=query)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Tasks did not stop", result.stderr)

    def test_fake_docker_does_not_discover_or_validate_bash(self):
        # Child CLI mode must work even when the inherited Bash discovery settings are invalid.
        # 即使继承的 Bash 查找配置无效，子进程 CLI 模式也必须正常工作。
        env = self.env | {"GIT_BASH_PATH": str(self.directory / "missing-bash.exe"), "PATH": ""}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "fake-docker", "stack", "config"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.state()["calls"]), 1)

    def test_setup_polling_bounds_list_state_and_exit_queries(self):
        # Stall in Bash so GNU timeout controls the child on both Linux and Git Bash.
        # 在 Bash 内阻塞，使 GNU timeout 在 Linux 和 Git Bash 上都能控制子进程。
        (self.bin / "docker").write_text(r'''#!/usr/bin/env bash
if grep -q '"launched": true' "$FAKE_STATE"; then
  case "$STALL_SETUP_QUERY:$*" in
    list:service\ ps*|state:inspect*Status.State*|exit:inspect*ContainerStatus.ExitCode*) sleep 30 ;;
  esac
fi
exec "$TEST_PYTHON" "$TEST_DRIVER" fake-docker "$@"
''', newline="\n")
        for query in ("list", "state", "exit"):
            with self.subTest(query=query):
                self.state_path.write_text(json.dumps({"calls": []}))
                self.env["STALL_SETUP_QUERY"] = query
                result = self.run_script(timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.state().get("apps", False))
                self.assertTrue(self.state()["setup"])
                self.assertEqual(len(self.mutations()), 2)
                self.assertIn("Setup timed out", result.stderr)

    def test_wait_healthy_accepts_probe_completed_before_deadline(self):
        result = self.run_simulated_health_probe(delay=0, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 healthy tasks", result.stdout)
        self.assertIn("health-result:healthy", self.probe_trace.read_text())

    def test_wait_healthy_rejects_healthy_probe_completed_after_deadline(self):
        result = self.run_simulated_health_probe(delay=2, timeout=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timeout", result.stderr.lower())
        self.assertNotIn("healthy tasks", result.stdout)
        self.assertIn("State.Health", self.probe_trace.read_text())
        self.assertIn("health-result:healthy", self.probe_trace.read_text())

    def test_wait_healthy_terminates_hanging_probe_within_bound(self):
        result = self.run_health_probe(delay=30, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timeout", result.stderr.lower())
        self.assertIn("State.Health", self.probe_trace.read_text())

    def test_wait_healthy_does_not_accept_unhealthy_probe(self):
        result = self.run_simulated_health_probe(
            delay=0,
            health="unhealthy",
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        trace = self.probe_trace.read_text()
        self.assertIn("health-result:unhealthy", trace)
        self.assertIn("Health/convergence timeout", result.stderr)
        self.assertNotIn("healthy tasks", result.stdout)

    def run_worker_wait(self, **scenario):
        self.two_nodes()
        self.state_path.write_text(json.dumps({"apps": True, "calls": [], **scenario}))
        # Advance to the deadline after two unsuccessful polls, without a real sleep.
        # 两轮检查均失败后推进至截止时间，无须实际休眠。
        command = r'''
set -eu
export PATH="$TEST_BIN:$PATH" MANAGER_ID=manager WORKER_ID=worker DEPLOY_TIMEOUT=60
source deploy/scripts/common.sh
polls=0
sleep() { polls=$((polls + 1)); if (( polls == 2 )); then SECONDS=$deadline; fi; }
timeout() { printf '%s %s\n' "$3" "$4" >> "$PROBE_TRACE"; command timeout "$@"; }
wait_healthy "${STACK}_worker" 2
'''
        self.probe_trace = self.directory / "probe-trace.log"
        self.probe_trace.write_text("")
        env = self.env | {"PROBE_TRACE": bash_path(self.probe_trace)}
        return subprocess.run([BASH, "-c", command], env=env, cwd=ROOT, capture_output=True, text=True, timeout=20)

    def test_ssh_reading_stdin_does_not_skip_the_second_healthy_task(self):
        # SSH may consume its entire stdin before returning the first task's health.
        # SSH 可能在返回首个任务的健康状态前读尽标准输入。
        (self.bin / "ssh").write_text(
            '#!/usr/bin/env bash\ncat >/dev/null\nexport FAKE_NODE=worker\nexec bash -c "${@: -1}"\n', newline="\n"
        )
        result = self.run_worker_wait(update="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 healthy tasks", result.stdout)
        inspected = [
            call["args"][-1] for call in self.state()["calls"]
            if call["args"][:2] == ["inspect", "--format"]
        ]
        self.assertEqual(inspected, ["container-worker-1", "container-worker-2"])
        probes = [line.split() for line in self.probe_trace.read_text().splitlines()]
        self.assertEqual([command for _, command in probes].count("ssh"), 1)
        self.assertTrue(all(0 < int(duration.removesuffix("s")) <= 10 for duration, _ in probes))

    def test_transient_worker_queries_restart_the_health_check(self):
        service = self.env["STACK"] + "_worker"
        for query in (
            ["service", "inspect", service], ["service", "ps", service],
            ["inspect", "--type", "worker-2"], ["inspect", "--format", "container-worker-1"],
        ):
            with self.subTest(query=query):
                result = self.run_worker_wait(fail_query=query)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("2 healthy tasks", result.stdout)
                self.assertIn("unavailable", result.stderr)
                state = self.state()
                self.assertTrue(state["query_failed"])
                self.assertEqual(sum(call["args"][:2] == ["service", "inspect"] for call in state["calls"]), 2)

    def test_worker_timeout_reports_last_observation(self):
        for scenario, reason in (
            ({"fail_query": ["service", "ps", self.env["STACK"] + "_worker"], "permanent": True}, "task list unavailable"),
            ({"fault": "unhealthy-worker"}, "unhealthy"),
            ({"update": "updating"}, "update state is updating"),
            ({"task_list": "worker-1"}, "1 desired tasks, expected 2"),
            ({"task_list": "worker-1\nworker-1"}, "duplicate task ID"),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_worker_wait(**scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Health/convergence timeout", result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertNotIn("healthy tasks", result.stdout)

    def test_failed_worker_update_stops_immediately(self):
        for update in ("paused", "failed", "rollback_started", "rollback_completed"):
            with self.subTest(update=update):
                result = self.run_worker_wait(update=update)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Update failed for offline-test_worker ({update})", result.stderr)
                self.assertEqual(len(self.state()["calls"]), 1)

    def test_expired_deadline_does_not_start_a_probe(self):
        command = 'source "$COMMON_SH"; probe_before_deadline "$((SECONDS - 1))" true; status=$?; test "$status" -eq 124'
        env = os.environ.copy() | {
            "COMMON_SH": bash_path(ROOT / "deploy/scripts/common.sh"),
        }
        result = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


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
        self.two_nodes()
        result = self.run_script(fault="existing-database")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mutations = self.mutations()
        self.assertEqual(len(mutations), 4)
        self.assertEqual(mutations[0]["args"][:2], ["stack", "deploy"])
        self.assertEqual((mutations[0]["api"], mutations[0]["worker"]), ("0", "0"))
        self.assertEqual(mutations[1]["args"], ["service", "update", "--detach=true", "--replicas", "1", "--force", "offline-test_setup"])
        self.assertIn("offline-test_setup=0", mutations[2]["args"])
        self.assertIn("offline-test_api=2", mutations[3]["args"])
        self.assertIn("offline-test_worker=2", mutations[3]["args"])
        self.assertFalse(self.state()["setup"])
        self.assertIn("exit code 0", result.stdout)
        for service in ("api", "worker"):
            self.assertIn(f"offline-test_{service}: 2 healthy tasks", result.stdout)

    def test_failed_setup_never_starts_applications_or_retries(self):
        for fault in ("failed-setup", "nonzero-setup", "duplicate-setup", "timeout-setup", "historical-only"):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({"calls": []}))
                result = self.run_script(fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.state().get("apps", False))
                self.assertTrue(self.state()["setup"])
                self.assertEqual(len(self.mutations()), 2)

    def test_setup_requires_successful_replica_and_restart_policy_inspections(self):
        for fault in ("failed-replica-inspection", "failed-restart-inspection", "retry-enabled"):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({"calls": []}))
                result = self.run_script(fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.state().get("launched"))
                self.assertFalse(self.state().get("apps"))

    def test_second_setup_task_visible_at_scale_down_blocks_startup(self):
        result = self.run_script(fault="late-duplicate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple new setup tasks", result.stderr)
        self.assertFalse(self.state().get("apps"))
        self.assertEqual(sum(call["args"][:2] == ["service", "update"] for call in self.mutations()), 1)

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

    def test_existing_postgres_image_comparison(self):
        self.two_nodes()
        digest = "@sha256:837bed18782820fe967067cb5328b02b0d9992c1be9a37ee0c9c492cfc03af3b"
        self.env["POSTGRES_IMAGE"] = "docker.io/library/postgres" + digest
        for service_image, equivalent in (
            ("postgres" + digest, True),
            ("library/postgres" + digest, True),
            ("docker.io/postgres" + digest, True),
            ("index.docker.io/library/postgres:17" + digest, True),
            ("postgres" + digest[:-1] + "a", False),
            ("postgres:17", False),
            ("docker.io/team/postgres" + digest, False),
            ("registry.example/library/postgres" + digest, False),
            ("docker.io/library/redis" + digest, False),
        ):
            with self.subTest(service_image=service_image):
                self.state_path.write_text(json.dumps({"calls": [], "db_image": service_image}))
                result = self.run_script(script="preflight.sh", mode="--check", fault="existing-database")
                if equivalent:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Existing database settings", result.stderr)
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


class ReleaseWorkflowTests(unittest.TestCase):
    def test_digest_summary_uses_full_verified_image_reference(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('Deploy by digest: `%s`', workflow)
        self.assertIn('"$image@$digest"', workflow)
        self.assertIn('docker pull "$image@$digest"', workflow)
        self.assertIn('docker image inspect "$image@$digest"', workflow)



class GitBashDiscoveryTests(unittest.TestCase):
    def test_configured_path_takes_precedence_and_preserves_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "Git Tools" / "bash.exe"
            configured.parent.mkdir()
            configured.touch()

            def unexpected_discovery(command):
                self.fail(f"Unexpected PATH lookup for {command}")

            fake_result = subprocess.CompletedProcess(
                args=[str(configured), "--version"],
                returncode=0,
                stdout="GNU bash, version 5.2.0\n",
                stderr="",
            )

            with patch("subprocess.run", return_value=fake_result):
                self.assertEqual(
                    resolve_bash_path(
                        {"GIT_BASH_PATH": str(configured)},
                        "nt",
                        unexpected_discovery,
                    ),
                    str(configured),
                )

    def test_invalid_configured_path_explains_the_variable(self):
        with self.assertRaisesRegex(RuntimeError, "GIT_BASH_PATH"):
            resolve_bash_path({"GIT_BASH_PATH": "missing bash.exe"}, "nt")

    def test_non_executable_configured_path_explains_the_variable(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "bash.exe"
            configured.touch()
            with patch("subprocess.run", side_effect=PermissionError("not executable")):
                with self.assertRaisesRegex(RuntimeError, "GIT_BASH_PATH.*working Bash"):
                    resolve_bash_path({"GIT_BASH_PATH": str(configured)}, "nt")

    def test_discovers_bash_relative_to_git_executable_without_searching_for_wsl_bash(self):
        with tempfile.TemporaryDirectory() as temporary:
            git_root = Path(temporary) / "Git Tools"
            git_executable = git_root / "cmd" / "git.exe"
            bash_executable = git_root / "bin" / "bash.exe"
            git_executable.parent.mkdir(parents=True)
            bash_executable.parent.mkdir(parents=True)
            git_executable.touch()
            bash_executable.touch()
            lookups = []

            def find_executable(command):
                lookups.append(command)
                return str(git_executable) if command == "git" else None

            self.assertEqual(
                resolve_bash_path({}, "nt", find_executable), str(bash_executable)
            )
            self.assertEqual(lookups, ["git"])

    def test_falls_back_to_both_windows_program_files_locations(self):
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                install_root = Path(temporary) / "Windows Git"
                bash_executable = install_root / "Git" / "bin" / "bash.exe"
                bash_executable.parent.mkdir(parents=True)
                bash_executable.touch()

                def no_git(_command):
                    return None

                self.assertEqual(
                    resolve_bash_path({variable: str(install_root)}, "nt", no_git),
                    str(bash_executable),
                )

    def test_unavailable_git_bash_reports_how_to_configure_it(self):
        with self.assertRaisesRegex(RuntimeError, "GIT_BASH_PATH"):
            resolve_bash_path({}, "nt", lambda _command: None)

    def test_linux_ci_still_discovers_bash_from_path(self):
        lookups = []

        def find_executable(command):
            lookups.append(command)
            return "/usr/bin/bash"

        self.assertEqual(resolve_bash_path({}, "posix", find_executable), "/usr/bin/bash")
        self.assertEqual(lookups, ["bash"])


if __name__ == "__main__":
    unittest.main()
