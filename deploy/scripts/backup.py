"""Back up the manager's PostgreSQL task to private DigitalOcean Spaces."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import os
import re
import subprocess
import sys
import tempfile
import time

from backup_common import ARCHIVE_NAME, OperationFailed, archive_digest, docker


class ManagerBusy(OperationFailed):
    """Temporary contention with a deployment or another backup."""


def dump_database(stack, archive):
    tasks = docker(
        "ps", "--no-trunc", "--quiet", "--filter", f"label=com.docker.stack.namespace={stack}",
        "--filter", f"label=com.docker.swarm.service.name={stack}_db",
    ).decode().splitlines()
    if len(tasks) != 1 or not re.fullmatch("[a-f0-9]{64}", tasks[0]):
        raise OperationFailed("Expected exactly one healthy local Swarm database task")
    task = tasks[0]
    if docker("inspect", "--format", "{{.State.Health.Status}}", task).strip() != b"healthy":
        raise OperationFailed("Database task is unhealthy")
    if not re.match(rb"pg_dump \(PostgreSQL\) 17\.", docker("exec", task, "pg_dump", "--version")):
        raise OperationFailed("Expected PostgreSQL 17 dump tools")
    # Read the existing Swarm secret only inside the task.
    # 仅在数据库任务内部读取已有的 Swarm 密钥。
    command = (
        'PGPASSWORD=$(cat /run/secrets/db_password) && test -n "$PGPASSWORD" && export PGPASSWORD && '
        'exec pg_dump --no-password --format=custom --host=127.0.0.1 '
        '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
    )
    docker("exec", task, "sh", "-ec", command, stdout=archive, timeout=1800)
    archive.seek(0)
    # Decode the full archive to a sink. Script output mode opens no database connection.
    # 完整解码归档并丢弃输出，生成 SQL 脚本的模式不会连接数据库。
    docker("exec", "-i", task, "pg_restore", "--format=custom", "--file=/dev/null",
           stdin=archive, stdout=subprocess.DEVNULL, timeout=1800)
    archive.seek(0)


def inventory(spaces, bucket, prefix, today):
    days = {}
    pages = spaces.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.startswith(prefix):
                continue
            match = ARCHIVE_NAME.fullmatch(key[len(prefix):])
            if match:
                day = date.fromisoformat(match[1])
                if day > today or day in days:
                    raise OperationFailed("Future or duplicate backup date. Inspect the inventory")
                days[day] = key
    return days


def verify_remote(spaces, bucket, key):
    expected = ARCHIVE_NAME.fullmatch(key.rsplit("/", 1)[1])[2]
    with closing(spaces.get_object(Bucket=bucket, Key=key)["Body"]) as body:
        if archive_digest(body) != expected:
            raise OperationFailed(f"Archive checksum mismatch for {key}")


def daily_backup(spaces, bucket, stack, today):
    prefix = f"agenthub-backups/{stack}/postgres17/daily/"
    if spaces.get_bucket_versioning(Bucket=bucket).get("Status"):
        raise OperationFailed("Daily retention requires a dedicated unversioned bucket")
    print("Listing daily archives in Spaces.", flush=True)
    days = inventory(spaces, bucket, prefix, today)
    if today not in days:
        with tempfile.TemporaryFile(dir=os.environ.get("TMPDIR")) as archive:
            print("Creating and checking the PostgreSQL archive.", flush=True)
            dump_database(stack, archive)
            digest = archive_digest(archive)
            size = archive.tell()
            if size > 4 * 1024**3:
                raise OperationFailed("Archive exceeds the 4 GiB single-upload limit for this job")
            archive.seek(0)
            key = f"{prefix}{today.isoformat()}-{digest}.dump"
            print("Uploading the private daily archive.", flush=True)
            spaces.put_object(Bucket=bucket, Key=key, Body=archive, ContentLength=size, ACL="private")
        days[today] = key

    newest = sorted(days, reverse=True)
    expired = newest[7:]
    # Read back every survivor before deleting even one older restore point.
    # 删除任何旧恢复点前，先回读并校验全部七个保留副本。
    survivors_to_check = newest[:7] if expired else [today]
    print(f"Checking {len(survivors_to_check)} remote archive(s).", flush=True)
    for day in survivors_to_check:
        verify_remote(spaces, bucket, days[day])
    for day in reversed(expired):
        print(f"Pruning the expired archive for {day}.", flush=True)
        spaces.delete_object(Bucket=bucket, Key=days[day])
    print(f"Verified {days[today]}. Retained {min(len(days), 7)} daily backups, pruned {len(expired)}.")


def run():
    import fcntl
    import boto3
    from botocore.config import Config

    stack = os.environ["STACK"]
    bucket = os.environ["SPACES_BUCKET"]
    region = os.environ["SPACES_REGION"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", stack):
        raise OperationFailed("Invalid STACK")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise OperationFailed("Invalid SPACES_BUCKET")
    if not re.fullmatch(r"[a-z]{3}[0-9]+", region):
        raise OperationFailed("Invalid SPACES_REGION")
    lock_path = os.environ.get("DEPLOY_LOCK_PATH", "/var/lock/agenthub-deploy.lock")
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagerBusy("Another backup or deployment holds the manager lock") from None
        spaces = boto3.Session(profile_name="agenthub-backup").client(
            "s3", region_name="us-east-1", endpoint_url=f"https://{region}.digitaloceanspaces.com",
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=60,
                          retries={"mode": "standard", "total_max_attempts": 3},
                          request_checksum_calculation="when_required"),
        )
        daily_backup(spaces, bucket, stack, datetime.now(timezone.utc).date())


def run_with_lock_retries(attempts):
    for attempt in range(1, attempts + 1):
        try:
            run()
            return
        except ManagerBusy:
            if attempt == attempts:
                raise
            print(f"Manager lock busy on attempt {attempt}/{attempts}. Retrying in 30 minutes.", flush=True)
            time.sleep(30 * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Describe the job offline (default)")
    mode.add_argument("--run", action="store_true", help="Perform one backup and retention attempt")
    mode.add_argument("--scheduled", action="store_true", help="Run the backup with bounded retries for a busy manager lock")
    args = parser.parse_args(argv)
    if not (args.run or args.scheduled):
        print("Plan only. Dump PostgreSQL 17, upload privately, verify and retain seven UTC dates.")
        return 0
    try:
        run_with_lock_retries(4 if args.scheduled else 1)
    except ManagerBusy as exc:
        print(exc, file=sys.stderr)
        return 75
    except OperationFailed as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        # SDK and subprocess exceptions can contain private request or command details.
        # SDK 和子进程异常可能包含私有请求或命令细节。
        print(f"Backup failed ({type(exc).__name__}). Check configuration, storage and Spaces access.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
