"""Local Docker and archive handling shared by the cloud operations commands."""

import hashlib
import re
import subprocess


ARCHIVE_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-f0-9]{64})\.dump")


class OperationFailed(Exception):
    """An operator-facing diagnostic safe to write to the journal."""


def docker(*args, stdin=None, stdout=subprocess.PIPE, timeout=60):
    # Always address the local Linux daemon. Never log raw Docker/SQL output.
    # 始终连接本机 Linux 守护进程，不记录 Docker 或 SQL 的原始输出。
    try:
        return subprocess.run(
            ["docker", "--host", "unix:///var/run/docker.sock", *args],
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=stdout, stderr=subprocess.DEVNULL, check=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationFailed(f"Docker {args[0]} failed or timed out") from exc


def archive_digest(stream):
    if stream.read(5) != b"PGDMP":
        raise OperationFailed("Expected a PostgreSQL custom-format archive")
    digest = hashlib.sha256(b"PGDMP")
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()
