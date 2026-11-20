"""Restore one AgentHub daily archive into a disposable PostgreSQL 17 container."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from backup_common import ARCHIVE_NAME, OperationFailed, archive_digest, docker


DATABASE = "agenthub_restore_test"
CONNECTION = ("--host=/var/run/postgresql", "--username=postgres", f"--dbname={DATABASE}")


def create_container(name, image):
    try:
        container = docker(
            "create", "--pull=never", "--network=none", "--name", name,
            "--label", "agenthub.restore-test=true", "--label", f"agenthub.restore-run={name}",
            "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env", f"POSTGRES_DB={DATABASE}", "--mount", "type=volume,dst=/var/lib/postgresql/data",
            image, "postgres", "-c", "listen_addresses=",
        ).strip()
        if not re.fullmatch(rb"[a-f0-9]{64}", container):
            raise OperationFailed("Docker did not return the new container ID")
        return container.decode("ascii")
    except (OperationFailed, KeyboardInterrupt):
        # The daemon may have created the container before the client timed out.
        # 客户端超时时，守护进程可能已经创建容器。仅清理本次创建的实例。
        try:
            identity = docker("inspect", "--type", "container", "--format",
                              '{{.Id}} {{index .Config.Labels "agenthub.restore-run"}}', name)
        except OperationFailed:
            identity = b""
        owned = re.fullmatch(rb"([a-f0-9]{64}) " + name.encode("ascii"), identity.strip())
        if owned:
            docker("rm", "-fv", owned[1].decode("ascii"))
        raise


def restore_test(path, image):
    if not re.fullmatch(r"(?:(?:index\.)?docker\.io/)?(?:library/)?postgres(?::17(?:[.-][\w.-]+)?)?@sha256:[0-9a-f]{64}", image):
        raise OperationFailed("Use the approved digest of the official PostgreSQL 17 image")
    match = ARCHIVE_NAME.fullmatch(path.name)
    if not match or not path.is_file():
        raise OperationFailed("Supply a local daily archive with its original checksum-bearing filename")
    with path.open("rb") as archive:
        if archive_digest(archive) != match[2]:
            raise OperationFailed("Local archive checksum mismatch")
        archive.seek(0)
        if docker("info", "--format", "{{.Swarm.LocalNodeState}}").strip() != b"inactive":
            raise OperationFailed("Use an independent Docker daemon with Swarm inactive")
        name = f"agenthub-restore-test-{uuid4().hex}"
        print(f"Creating isolated restore container {name}", flush=True)
        container = create_container(name, image)
        try:
            docker("start", container)
            # Wait for PID 1 to be the final postmaster, past entrypoint initialization.
            # 等到初始化结束且最终的 postgres 进程成为 PID 1。
            docker("exec", container, "sh", "-ec",
                   'until [ "$(cat /proc/1/comm)" = postgres ] && '
                   'pg_isready -q -h /var/run/postgresql -U postgres -d "$POSTGRES_DB"; '
                   'do sleep 1; done', timeout=120)
            if not re.match(rb"pg_restore \(PostgreSQL\) 17\.", docker("exec", container, "pg_restore", "--version")):
                raise OperationFailed("Expected PostgreSQL 17 restore tools")
            docker("exec", "-i", container, "pg_restore", "--format=custom", "--exit-on-error",
                   "--single-transaction", "--no-owner", "--no-privileges", *CONNECTION,
                   stdin=archive, stdout=subprocess.DEVNULL, timeout=1800)
            result = docker(
                "exec", container, "psql", "-XAt", "--set=ON_ERROR_STOP=1", *CONNECTION, "-c",
                "SELECT current_setting('server_version_num')::int / 10000 = 17 "
                "AND EXISTS (SELECT 1 FROM public.alembic_version) "
                "AND to_regclass('public.messages') IS NOT NULL "
                "AND to_regclass('public.jobs') IS NOT NULL",
            )
            if result.strip() != b"t":
                raise OperationFailed("Restored database failed AgentHub schema checks")
        finally:
            docker("rm", "-fv", container)
    print("Restore passed. Verified AgentHub schema and removed the test container and its storage.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Downloaded daily .dump with its original filename")
    parser.add_argument("--image", help="Locally cached official PostgreSQL 17 image pinned by digest")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Describe the drill offline (default)")
    mode.add_argument("--run", action="store_true", help="Run the disposable restore drill")
    args = parser.parse_args(argv)
    if not args.run:
        print("Plan only. Verify archive, restore in a fresh network-isolated container, check schema and remove it.")
        return 0
    if not args.archive or not args.image:
        parser.error("--run requires --archive and --image")
    try:
        restore_test(args.archive, args.image)
    except (OperationFailed, OSError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
