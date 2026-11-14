#!/usr/bin/env python3
"""Read-only checks for AgentHub API and Docker Swarm service health."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


class MonitorFailed(Exception):
    """A concise diagnostic that does not include command or response bodies."""


def require(condition, message):
    if not condition:
        raise MonitorFailed(message)


def validate_url(value):
    url = urllib.parse.urlsplit(value)
    require(url.scheme in ("http", "https") and url.hostname, "Expected an HTTP(S) API URL")
    require(not url.username and not url.password, "Credentials must not appear in the URL")
    require(not url.query and not url.fragment and url.path in ("", "/"), "Use the API origin without a path, query or fragment")
    require(url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"), "Use HTTPS or a loopback SSH tunnel")
    return value.rstrip("/")


def run_docker(args, timeout):
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MonitorFailed("Docker command failed or timed out") from None
    require(result.returncode == 0, "Docker command failed; verify manager access and Swarm state")
    return result.stdout


def run_remote_docker(ssh_target, args, timeout):
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", ssh_target)), "Invalid WORKER_SSH target")
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", ssh_target, shlex.join(["docker", *args])],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MonitorFailed("Remote Docker inspection failed or timed out") from None
    require(result.returncode == 0, "Remote Docker inspection failed; verify WORKER_SSH access")
    return result.stdout


def check_api(base_url, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(base_url + "/health", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            require(response.status == 200, "API health endpoint returned an unexpected status")
            health = json.load(response)
    except urllib.error.HTTPError as error:
        raise MonitorFailed(f"API health endpoint returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise MonitorFailed("API health request failed or returned invalid JSON") from None
    require(
        isinstance(health, dict) and health.get("status") == "ok" and health.get("database") == "ok",
        "API or PostgreSQL connectivity health check failed",
    )
    return health


def parse_services(output, stack, expected):
    observed = {}
    for line in output.splitlines():
        parts = line.split("|", 1)
        require(len(parts) == 2, "Docker returned an invalid service list")
        name, replicas = parts
        prefix = stack + "_"
        if not name.startswith(prefix):
            continue
        service = name[len(prefix) :]
        if service not in expected:
            continue
        counts = replicas.split("/", 1)
        require(len(counts) == 2 and all(item.isdigit() for item in counts), "Docker returned invalid replica counts")
        observed[service] = (int(counts[0]), int(counts[1]))
    require(set(observed) == set(expected), "One or more AgentHub Swarm services are missing")
    for service, desired in expected.items():
        running, configured = observed[service]
        require(configured == desired and running == desired, f"{service} replicas are {running}/{configured}; expected {desired}/{desired}")
    return observed


def check_nodes(output, expected_nodes):
    nodes = {}
    for line in output.splitlines():
        fields = line.split("|", 4)
        require(len(fields) == 5, "Docker returned an invalid node list")
        node_id, hostname, status, availability, _manager_status = fields
        require(node_id and hostname, "Docker returned an invalid node identity")
        require(status == "Ready" and availability == "Active", f"Swarm node {hostname} is not Ready and Active")
        require(node_id not in nodes and hostname not in nodes.values(), "Docker returned duplicate Swarm nodes")
        nodes[node_id] = hostname
    require(bool(nodes), "Swarm has no nodes")
    require(len(nodes) == expected_nodes, f"Swarm has {len(nodes)} nodes; expected {expected_nodes}")
    return nodes


def parse_tasks(output, service, expected_count):
    tasks = []
    for line in output.splitlines():
        fields = line.split("|", 1)
        require(len(fields) == 2 and all(fields), f"Docker returned an invalid {service} task list")
        tasks.append((fields[0], fields[1]))
    require(len(tasks) == expected_count, f"{service} has {len(tasks)} running task(s); expected {expected_count}")
    require(len({task_id for task_id, _ in tasks}) == len(tasks), f"Docker returned duplicate {service} tasks")
    return tasks


def check_task_health(service, tasks, nodes, manager_id, ssh_target, timeout):
    require(manager_id in nodes, "The local Docker daemon is not a listed Swarm node")
    for task_id, listed_hostname in tasks:
        task_output = run_docker(
            ["inspect", "--type", "task", "--format", "{{.Status.State}}|{{.NodeID}}|{{if .Status.ContainerStatus}}{{.Status.ContainerStatus.ContainerID}}{{end}}", task_id],
            timeout,
        ).strip()
        fields = task_output.split("|", 2)
        require(len(fields) == 3, f"Docker returned invalid {service} task details")
        task_state, node_id, container_id = fields
        require(task_state == "running", f"{service} task {task_id} is {task_state}, not running")
        require(node_id in nodes and nodes[node_id] == listed_hostname, f"{service} task node does not match Swarm inventory")
        require(container_id, f"{service} task {task_id} has no container ID")
        inspect_args = [
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            container_id,
        ]
        if node_id == manager_id:
            container_output = run_docker(inspect_args, timeout).strip()
        else:
            require(ssh_target, f"{service} has a task on {listed_hostname}; set WORKER_SSH for remote health inspection")
            remote_id = run_remote_docker(ssh_target, ["info", "--format", "{{.Swarm.NodeID}}"], timeout).strip()
            require(remote_id == node_id, "WORKER_SSH reaches a different Swarm node")
            container_output = run_remote_docker(ssh_target, inspect_args, timeout).strip()
        container_fields = container_output.split("|", 1)
        require(len(container_fields) == 2, f"Docker returned invalid {service} container health")
        container_state, health = container_fields
        require(container_state == "running", f"{service} container on {listed_hostname} is {container_state}")
        require(health == "healthy", f"{service} container on {listed_hostname} health is {health}; expected healthy")


def check_service_tasks(stack, service, expected_count, nodes, manager_id, ssh_target, timeout):
    output = run_docker(
        ["service", "ps", "--no-trunc", "--filter", "desired-state=running", "--format", "{{.ID}}|{{.Node}}", f"{stack}_{service}"],
        timeout,
    )
    tasks = parse_tasks(output, service, expected_count)
    check_task_health(service, tasks, nodes, manager_id, ssh_target, timeout)
    return len(tasks)


def positive_env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        parsed = int(value)
    except ValueError:
        raise MonitorFailed(f"{name} must be a positive integer") from None
    require(parsed > 0, f"{name} must be a positive integer")
    return parsed


def monitor(stack, base_url, api_replicas, worker_replicas, expected_nodes, timeout):
    health = check_api(base_url, timeout)
    service_output = run_docker(
        ["service", "ls", "--filter", f"label=com.docker.stack.namespace={stack}", "--format", "{{.Name}}|{{.Replicas}}"],
        timeout,
    )
    replicas = parse_services(
        service_output,
        stack,
        {"api": api_replicas, "worker": worker_replicas, "db": 1, "setup": 0},
    )
    node_output = run_docker(
        ["node", "ls", "--format", "{{.ID}}|{{.Hostname}}|{{.Status}}|{{.Availability}}|{{.ManagerStatus}}"],
        timeout,
    )
    nodes = check_nodes(node_output, expected_nodes)
    manager_id = run_docker(["info", "--format", "{{.Swarm.NodeID}}"], timeout).strip()
    ssh_target = os.environ.get("WORKER_SSH", "")
    for service in ("api", "worker", "db"):
        count = check_service_tasks(stack, service, replicas[service][0], nodes, manager_id, ssh_target, timeout)
        require(count == replicas[service][0], f"{service} task health check did not cover every running replica")
    return health, replicas, nodes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Describe checks without network or Docker access")
    parser.add_argument("--stack", default=os.environ.get("STACK", "agenthub"))
    parser.add_argument("--url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:18000"))
    parser.add_argument("--api-replicas", type=int)
    parser.add_argument("--worker-replicas", type=int)
    parser.add_argument("--expected-nodes", type=int, default=2, help="Expected Ready and Active Swarm nodes (1 or 2)")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds")
    args = parser.parse_args(argv)
    try:
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.stack)), "Invalid Swarm stack name")
        api_replicas = args.api_replicas if args.api_replicas is not None else positive_env_int("API_REPLICAS", 2)
        worker_replicas = args.worker_replicas if args.worker_replicas is not None else positive_env_int("WORKER_REPLICAS", 2)
        require(api_replicas > 0 and worker_replicas > 0, "Application replica counts must be positive")
        require(args.expected_nodes in (1, 2), "--expected-nodes must be 1 or 2")
        require(1 <= args.timeout <= 120, "Timeout must be between 1 and 120 seconds")
        base_url = validate_url(args.url)
        if args.plan:
            print("PLAN: GET /health, read AgentHub service replica counts, inspect API/worker/PostgreSQL task and container health on their Swarm nodes, and check the expected node count/readiness. No changes made.")
            return 0
        health, replicas, nodes = monitor(args.stack, base_url, api_replicas, worker_replicas, args.expected_nodes, args.timeout)
        print(f"PASS: API and PostgreSQL connectivity; reply_mode={health.get('reply_mode', 'unknown')}")
        print("PASS: " + ", ".join(f"{name}={running}/{desired}" for name, (running, desired) in replicas.items()))
        print("PASS: every running API, worker, and PostgreSQL task/container is healthy")
        print(f"PASS: {len(nodes)} Swarm node(s) Ready and Active")
        return 0
    except (MonitorFailed, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
