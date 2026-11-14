"""Offline tests for read-only monitoring checks."""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from deploy.scripts import monitor


SERVICES = """agenthub_api|2/2
agenthub_worker|2/2
agenthub_db|1/1
agenthub_setup|0/0
other_api|9/9
"""
NODES_TWO = "manager-id|manager|Ready|Active|Leader\nworker-id|worker|Ready|Active|\n"
NODES_ONE = "manager-id|manager|Ready|Active|Leader\n"
EXPECTED = {"api": 2, "worker": 2, "db": 1, "setup": 0}


class DockerFixture:
    def __init__(self, unhealthy="", stopped_task="", one_node=False):
        self.unhealthy = unhealthy
        self.stopped_task = stopped_task
        self.one_node = one_node
        self.remote_inspections = []

    def run(self, args, _timeout):
        if args[:2] == ["service", "ls"]:
            return SERVICES
        if args[:2] == ["node", "ls"]:
            return NODES_ONE if self.one_node else NODES_TWO
        if args[:1] == ["info"]:
            return "manager-id"
        if args[:2] == ["service", "ps"]:
            service = args[-1].rsplit("_", 1)[-1]
            ids = {"api": ["api-a", "api-b"], "worker": ["worker-a", "worker-b"], "db": ["db-a"]}[service]
            return "".join(f"{task}|{'worker' if task == 'api-b' else 'manager'}\n" for task in ids)
        if args[:3] == ["inspect", "--type", "task"]:
            task = args[-1]
            state = "starting" if task == self.stopped_task else "running"
            node = "worker-id" if task == "api-b" else "manager-id"
            return f"{state}|{node}|container-{task}"
        if args[0] == "inspect":
            container = args[-1]
            health = "unhealthy" if container == f"container-{self.unhealthy}" else "healthy"
            return f"running|{health}"
        raise AssertionError(f"Unexpected read-only Docker command: {args}")

    def run_remote(self, _target, args, timeout):
        if args[:1] == ["info"]:
            return "worker-id"
        return self.run(args, timeout)


class MonitorTests(unittest.TestCase):
    def test_plan_does_not_contact_api_or_docker(self):
        with patch.object(monitor, "monitor", side_effect=AssertionError("network access")):
            self.assertEqual(monitor.main(["--plan"]), 0)

    def test_expected_replica_counts_are_parsed_for_stack_only(self):
        result = monitor.parse_services(SERVICES, "agenthub", EXPECTED)
        self.assertEqual(result["api"], (2, 2))
        self.assertEqual(result["setup"], (0, 0))

    def test_missing_or_unhealthy_service_replica_count_fails(self):
        for value in ("agenthub_api|1/2\n", SERVICES.replace("agenthub_worker|2/2\n", ""), SERVICES.replace("agenthub_db|1/1", "agenthub_db|0/1")):
            with self.subTest(value=value), self.assertRaises(monitor.MonitorFailed):
                monitor.parse_services(value, "agenthub", EXPECTED)

    def test_node_readiness_and_configurable_expected_count(self):
        self.assertEqual(monitor.check_nodes(NODES_ONE, 1), {"manager-id": "manager"})
        self.assertEqual(len(monitor.check_nodes(NODES_TWO, 2)), 2)
        for value, count in ((NODES_TWO, 1), ("id|manager|Down|Active|Leader\n", 1), ("", 1)):
            with self.subTest(count=count), self.assertRaises(monitor.MonitorFailed):
                monitor.check_nodes(value, count)

    def test_monitor_checks_running_tasks_and_container_health_on_both_nodes(self):
        fixture = DockerFixture()
        with patch.object(monitor, "check_api", return_value={"status": "ok", "database": "ok", "reply_mode": "fixed"}), patch.object(
            monitor, "run_docker", side_effect=fixture.run
        ), patch.object(monitor, "run_remote_docker", side_effect=fixture.run_remote) as remote, patch.dict(os.environ, {"WORKER_SSH": "agenthub-worker"}):
            health, replicas, nodes = monitor.monitor("agenthub", "http://127.0.0.1:18000", 2, 2, 2, 10)
        self.assertEqual(health["database"], "ok")
        self.assertEqual(replicas["db"], (1, 1))
        self.assertEqual(len(nodes), 2)
        self.assertEqual(remote.call_count, 2)

    def test_container_health_is_not_inferred_from_running_task_state(self):
        for failed in ("api-a", "worker-a", "db-a"):
            with self.subTest(service=failed), patch.object(
                monitor, "run_docker", side_effect=DockerFixture(unhealthy=failed).run
            ), patch.object(monitor, "run_remote_docker", side_effect=DockerFixture(unhealthy=failed).run_remote), patch.object(
                monitor, "check_api", return_value={"status": "ok", "database": "ok"}
            ), patch.dict(os.environ, {"WORKER_SSH": "agenthub-worker"}):
                with self.assertRaisesRegex(monitor.MonitorFailed, "health is unhealthy"):
                    monitor.monitor("agenthub", "http://127.0.0.1:18000", 2, 2, 2, 10)

    def test_non_running_task_fails_even_if_container_health_would_be_healthy(self):
        fixture = DockerFixture(stopped_task="api-a")
        with patch.object(monitor, "run_docker", side_effect=fixture.run), patch.object(
            monitor, "run_remote_docker", side_effect=AssertionError("container inspect should not run")
        ), patch.object(monitor, "check_api", return_value={"status": "ok", "database": "ok"}):
            with self.assertRaisesRegex(monitor.MonitorFailed, "not running"):
                monitor.monitor("agenthub", "http://127.0.0.1:18000", 2, 2, 2, 10)

    def test_remote_task_requires_configured_ssh_target(self):
        with patch.object(monitor, "run_docker", side_effect=DockerFixture().run), patch.object(
            monitor, "check_api", return_value={"status": "ok", "database": "ok"}
        ), patch.dict(os.environ, {"WORKER_SSH": ""}):
            with self.assertRaisesRegex(monitor.MonitorFailed, "set WORKER_SSH"):
                monitor.monitor("agenthub", "http://127.0.0.1:18000", 2, 2, 2, 10)

    def test_one_node_local_validation_mode(self):
        fixture = DockerFixture(one_node=True)
        # In local validation all replicas are placed on the manager.
        def local_run(args, timeout):
            output = fixture.run(args, timeout)
            if args[:2] == ["service", "ps"]:
                service = args[-1].rsplit("_", 1)[-1]
                ids = {"api": ["api-a", "api-b"], "worker": ["worker-a", "worker-b"], "db": ["db-a"]}[service]
                return "".join(f"{task}|manager\n" for task in ids)
            if args[:3] == ["inspect", "--type", "task"]:
                task = args[-1]
                return f"running|manager-id|container-{task}"
            return output
        with patch.object(monitor, "check_api", return_value={"status": "ok", "database": "ok"}), patch.object(
            monitor, "run_docker", side_effect=local_run
        ), patch.object(monitor, "run_remote_docker", side_effect=AssertionError("unexpected remote task")):
            result = monitor.monitor("agenthub", "http://127.0.0.1:18000", 2, 2, 1, 10)
        self.assertEqual(len(result[2]), 1)

    def test_invalid_replica_environment_values_return_diagnostic_without_traceback(self):
        for name, value in (("API_REPLICAS", "two"), ("WORKER_REPLICAS", ""), ("API_REPLICAS", "0")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), patch.object(
                monitor, "monitor", side_effect=AssertionError("must fail before monitoring")
            ):
                errors = io.StringIO()
                with redirect_stderr(errors):
                    result = monitor.main(["--plan"])
                self.assertEqual(result, 1)
                self.assertIn(f"{name} must be a positive integer", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_invalid_url_and_replica_counts_fail_without_docker_access(self):
        with patch.object(monitor, "monitor", side_effect=AssertionError("Docker access")):
            for args in (["--url", "http://public.example"], ["--api-replicas", "0"], ["--expected-nodes", "3"]):
                with self.subTest(args=args), redirect_stderr(io.StringIO()):
                    self.assertEqual(monitor.main(args), 1)


if __name__ == "__main__":
    unittest.main()
