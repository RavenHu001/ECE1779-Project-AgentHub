"""Offline tests for the PostgreSQL backup and isolated restore drill."""

from configparser import ConfigParser
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup
import backup_common
import restore_test


TODAY = date(2026, 11, 19)
PREFIX = "agenthub-backups/test/postgres17/daily/"
DUMP = b"PGDMP" + bytes(range(256))
DIGEST = hashlib.sha256(DUMP).hexdigest()
IMAGE = "docker.io/library/postgres@sha256:" + "a" * 64
CONTAINER = "f" * 64
UNITS = Path(__file__).resolve().parents[1] / "systemd"


def key(age):
    return f"{PREFIX}{TODAY - timedelta(days=age)}-{DIGEST}.dump"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.spaces = Mock(spec=["get_bucket_versioning", "get_paginator", "get_object", "put_object", "delete_object"])
        self.spaces.get_bucket_versioning.return_value = {}
        self.spaces.get_paginator.return_value.paginate.side_effect = self.pages
        self.spaces.put_object.side_effect = self.put
        self.spaces.get_object.side_effect = self.get
        self.spaces.delete_object.side_effect = self.delete
        self.dump = self.enter_patch(backup, "dump_database", self.write_dump)

    def enter_patch(self, module, name, effect):
        p = patch.object(module, name, side_effect=effect)
        self.addCleanup(p.stop)
        return p.start()

    def write_dump(self, stack, stream):
        self.assertEqual(stack, "test")
        stream.write(DUMP)
        stream.seek(0)

    def pages(self, *, Bucket, Prefix):
        self.assertEqual((Bucket, Prefix), ("private-bucket", PREFIX))
        for name in list(self.objects):
            yield {"Contents": [{"Key": name}]}

    def put(self, *, Bucket, Key, Body, ContentLength, ACL):
        self.assertEqual((Bucket, ACL, ContentLength), ("private-bucket", "private", len(DUMP)))
        self.assertNotIn(Key, self.objects)
        self.objects[Key] = Body.read()

    def get(self, *, Bucket, Key):
        self.assertEqual(Bucket, "private-bucket")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete(self, *, Bucket, Key):
        self.assertEqual(Bucket, "private-bucket")
        del self.objects[Key]

    def run_backup(self):
        with redirect_stdout(io.StringIO()):
            backup.daily_backup(self.spaces, "private-bucket", "test", TODAY)

    def populate(self, ages=range(1, 9)):
        self.objects.update({key(age): DUMP for age in ages})

    def test_retains_seven_distinct_successes_and_ignores_unrelated_keys(self):
        self.populate([1, 3, 4, 5, 7, 10, 13, 14])
        unrelated = [key(80) + ".partial", key(81).replace(PREFIX, "different/"),
                     key(82).replace(PREFIX, PREFIX + "manual/"), PREFIX + "notes"]
        self.objects.update(dict.fromkeys(unrelated, b"unrelated"))
        self.run_backup()
        self.assertEqual(set(self.objects), {*unrelated, *(key(n) for n in (0, 1, 3, 4, 5, 7, 10))})
        self.assertEqual(self.spaces.get_object.call_count, 7)
        self.spaces.get_paginator.assert_called_once_with("list_objects_v2")
        self.run_backup()
        self.dump.assert_called_once()
        self.spaces.put_object.assert_called_once()

    def test_failed_dump_or_upload_never_prunes(self):
        self.populate()
        for failed in [self.dump, self.spaces.put_object]:
            with self.subTest(operation=failed):
                previous = failed.side_effect
                failed.side_effect = OSError("interrupted")
                with self.assertRaises(OSError):
                    self.run_backup()
                failed.side_effect = previous
                self.spaces.delete_object.assert_not_called()
        self.assertEqual(len(self.objects), 8)

    def test_unusable_dump_never_uploads(self):
        self.dump.side_effect = lambda stack, archive: None
        with self.assertRaises(backup.OperationFailed):
            self.run_backup()
        self.spaces.put_object.assert_not_called()

    def test_versioned_bucket_fails_before_mutation(self):
        for status in ["Enabled", "Suspended"]:
            self.spaces.get_bucket_versioning.return_value = {"Status": status}
            with self.assertRaisesRegex(backup.OperationFailed, "unversioned"):
                self.run_backup()
            self.spaces.get_paginator.assert_not_called()
            self.dump.assert_not_called()

    def test_late_listing_failure_prevents_mutations(self):
        def broken_pages(**kwargs):
            yield {"Contents": [{"Key": key(1)}]}
            raise OSError("second page failed")
        self.spaces.get_paginator.return_value.paginate.side_effect = broken_pages
        with self.assertRaises(OSError):
            self.run_backup()
        self.spaces.put_object.assert_not_called()
        self.spaces.delete_object.assert_not_called()

    def test_corrupt_new_or_retained_copy_prevents_any_delete(self):
        for age in [0, 6]:
            with self.subTest(age=age):
                self.populate(range(9))
                self.objects[key(age)] = b"PGDMPcorrupt"
                with self.assertRaisesRegex(backup.OperationFailed, "checksum"):
                    self.run_backup()
                self.spaces.delete_object.assert_not_called()
                self.spaces.put_object.assert_not_called()

    def test_corruption_during_upload_prevents_any_delete(self):
        self.populate()
        self.spaces.put_object.side_effect = lambda **args: self.objects.update({args["Key"]: b"bad"})
        with self.assertRaises(backup.OperationFailed):
            self.run_backup()
        self.spaces.delete_object.assert_not_called()

    def test_interrupted_prune_can_resume(self):
        self.populate(range(1, 10))
        def partial_delete(**args):
            if args["Key"] == key(8):
                raise OSError("retention interrupted")
            self.delete(**args)
        self.spaces.delete_object.side_effect = partial_delete
        with self.assertRaises(OSError):
            self.run_backup()
        self.assertEqual(set(self.objects), {key(n) for n in range(9)})
        self.spaces.delete_object.side_effect = self.delete
        self.run_backup()
        self.assertEqual(set(self.objects), {key(n) for n in range(7)})
        self.spaces.put_object.assert_called_once()

    def test_future_or_duplicate_dates_fail_closed(self):
        for extra in [key(-1), key(1).replace(DIGEST, "a" * 64)]:
            self.objects = {key(1): DUMP, extra: DUMP}
            with self.assertRaises(backup.OperationFailed):
                self.run_backup()
            self.dump.assert_not_called()
            self.spaces.delete_object.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_plans_make_no_calls(self):
        with patch.object(backup, "run") as live, patch.object(restore_test, "restore_test") as drill:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(backup.main([]), 0)
                self.assertEqual(restore_test.main([]), 0)
            live.assert_not_called()
            drill.assert_not_called()

    def test_dump_uses_task_secret_and_checks_archive(self):
        with patch.object(backup, "docker", side_effect=[
            CONTAINER.encode(), b"healthy", b"pg_dump (PostgreSQL) 17.7", None, None
        ]) as docker, tempfile.TemporaryFile() as archive:
            backup.dump_database("test", archive)
            ps, health, version, dump, decode = docker.call_args_list
            self.assertEqual(health.args[-1], CONTAINER)
            self.assertEqual(version.args, ("exec", CONTAINER, "pg_dump", "--version"))
            self.assertIn("label=com.docker.swarm.service.name=test_db", ps.args)
            self.assertIn("label=com.docker.stack.namespace=test", ps.args)
            self.assertEqual(dump.args[:4], ("exec", CONTAINER, "sh", "-ec"))
            self.assertIn("/run/secrets/db_password", dump.args[-1])
            self.assertIn("--format=custom", dump.args[-1])
            self.assertIs(dump.kwargs["stdout"], archive)
            self.assertEqual(decode.args, ("exec", "-i", CONTAINER, "pg_restore", "--format=custom", "--file=/dev/null"))
            self.assertIs(decode.kwargs["stdin"], archive)

    def test_missing_ambiguous_task_or_wrong_version_aborts(self):
        for replies in [[b""], [((CONTAINER + "\n") * 2).encode()],
                        [CONTAINER.encode(), b"unhealthy"],
                        [CONTAINER.encode(), b"healthy", b"pg_dump (PostgreSQL) 18.0"]]:
            with patch.object(backup, "docker", side_effect=replies), tempfile.TemporaryFile() as archive:
                with self.assertRaises(backup.OperationFailed):
                    backup.dump_database("test", archive)

    def test_command_runner_forces_local_daemon_and_suppresses_stderr(self):
        with patch.object(subprocess, "run") as run:
            backup_common.docker("exec", CONTAINER, "pg_dump", "--version")
        self.assertEqual(run.call_args.args[0][:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_private_sdk_error_does_not_reach_journal(self):
        with patch.object(backup, "run", side_effect=ValueError("private response details")):
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(backup.main(["--run"]), 1)
            self.assertNotIn("private response", output.getvalue())

    def test_timer_retries_only_temporary_lock_contention(self):
        unit = ConfigParser(strict=False, interpolation=None)
        unit.read(UNITS / "agenthub-backup.service")
        service = unit["Service"]
        interpreter, script, *scheduled = service["ExecStart"].split()
        self.assertEqual((interpreter, script, scheduled), (
            "/opt/agenthub-backup/venv/bin/python", "/opt/agenthub-backup/backup.py", ["--scheduled"]))
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(service["Restart"], "no")
        self.assertNotIn("RestartForceExitStatus", service)
        self.assertEqual(service["TimeoutStartSec"], "3h30min")

        busy = backup.ManagerBusy("busy")
        for label, args, outcomes, expected in (
            ("immediate success", scheduled, [None], 0),
            ("last attempt succeeds", scheduled, [busy, busy, busy, None], 0),
            ("contention exhausted", scheduled, [busy] * 4, 75),
            ("backup failure", scheduled, [backup.OperationFailed("invalid archive")], 1),
            ("network error after contention", scheduled, [busy, OSError("network")], 1),
            ("manual invocation", ["--run"], [busy], 75),
        ):
            with self.subTest(case=label):
                timeline = Mock()
                timeline.run.side_effect = outcomes
                with (patch.object(backup, "run", timeline.run), patch.object(backup.time, "sleep", timeline.sleep),
                      redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
                    self.assertEqual(backup.main(args), expected)
                self.assertEqual(timeline.mock_calls,
                                 [call.run(), *([call.sleep(1800), call.run()] * (len(outcomes) - 1))])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "native systemd-analyze unavailable")
    def test_systemd_accepts_backup_service_and_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = Path(tmp) / "agenthub-backup.service"
            unit = (UNITS / service.name).read_text()
            service.write_text(unit.replace("/opt/agenthub-backup/venv/bin/python", sys.executable))
            timer = Path(tmp) / "agenthub-backup.timer"
            shutil.copyfile(UNITS / timer.name, timer)
            result = subprocess.run(["systemd-analyze", "verify", "--man=no", str(service), str(timer)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_docker_timeout_returns_a_sanitized_diagnostic(self):
        with (patch.object(backup_common.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 1)),
              self.assertRaisesRegex(backup.OperationFailed, "timed out")):
            backup_common.docker("exec", CONTAINER, "pg_restore")

    def test_manager_lock_and_regional_spaces_client(self):
        modules = {name: Mock() for name in ("boto3", "botocore", "botocore.config", "fcntl")}
        fcntl = modules["fcntl"]
        fcntl.LOCK_EX, fcntl.LOCK_NB = 2, 4
        session = modules["boto3"].Session
        settings = {"STACK": "test", "SPACES_BUCKET": "private-bucket", "SPACES_REGION": "tor1"}
        with (patch.dict(sys.modules, modules), patch.dict(backup.os.environ, settings, clear=True),
              patch("builtins.open", mock_open()) as lock, patch.object(backup, "daily_backup") as daily):
            fcntl.flock.side_effect = BlockingIOError()
            with self.assertRaisesRegex(backup.OperationFailed, "manager lock"):
                backup.run()
            lock.assert_called_with("/var/lock/agenthub-deploy.lock", "a")
            session.assert_not_called()
            daily.assert_not_called()
            fcntl.flock.side_effect = None
            backup.run()
            session.assert_called_once_with(profile_name="agenthub-backup")
            client = session.return_value.client
            self.assertEqual(client.call_args.kwargs["endpoint_url"], "https://tor1.digitaloceanspaces.com")
            self.assertEqual(daily.call_args.args[:3], (client.return_value, "private-bucket", "test"))
            backup.os.environ["SPACES_REGION"] = "tor1.example.org/"
            with self.assertRaisesRegex(backup.OperationFailed, "SPACES_REGION"):
                backup.run()
            client.assert_called_once()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / key(0).rsplit("/", 1)[1]
        self.path.write_bytes(DUMP)
        self.fail_at = None
        patcher = patch.object(restore_test, "docker", side_effect=self.command)
        self.addCleanup(patcher.stop)
        self.docker = patcher.start()

    def command(self, *args, **kwargs):
        if self.fail_at in args:
            raise backup.OperationFailed("simulated failure")
        if args == ("info", "--format", "{{.Swarm.LocalNodeState}}"):
            return b"inactive\n"
        if args[0] == "create":
            return CONTAINER.encode()
        if args[0] == "inspect":
            return f"{CONTAINER} other-run".encode()
        self.assertIn(CONTAINER, args)
        if "--version" in args:
            return b"pg_restore (PostgreSQL) 17.7"
        if "psql" in args:
            return b"t\n"
        if "--single-transaction" in args:
            self.assertEqual(kwargs["stdin"].read(), DUMP)
        return b""

    def drill(self):
        with redirect_stdout(io.StringIO()):
            restore_test.restore_test(self.path, IMAGE)

    def test_only_new_offline_container_and_anonymous_storage_are_used(self):
        self.drill()
        calls = [c.args for c in self.docker.call_args_list]
        create = calls[1]
        self.assertIn("--network=none", create)
        self.assertIn("--pull=never", create)
        self.assertIn("type=volume,dst=/var/lib/postgresql/data", create)
        self.assertFalse(any("bind" in arg or arg in ("-v", "-p", "--publish") for arg in create))
        restore = next(args for args in calls if "--single-transaction" in args)
        self.assertIn("--host=/var/run/postgresql", restore)
        self.assertIn("--dbname=agenthub_restore_test", restore)
        self.assertNotIn("--create", restore)
        self.assertNotIn("--clean", restore)
        self.assertEqual(calls[-1], ("rm", "-fv", CONTAINER))

    def test_cleanup_on_start_readiness_restore_and_validation_errors(self):
        for self.fail_at in ["start", "sh", "--single-transaction", "psql"]:
            with self.subTest(stage=self.fail_at), self.assertRaises(backup.OperationFailed):
                self.drill()
            self.docker.assert_called_with("rm", "-fv", CONTAINER)

    def test_failed_create_never_removes_an_existing_container(self):
        self.fail_at = "create"
        with self.assertRaises(backup.OperationFailed):
            self.drill()
        self.assertEqual([call.args[0] for call in self.docker.call_args_list], ["info", "create", "inspect"])

    def test_restore_refuses_swarm_or_unknown_daemon_before_create(self):
        for state in (b"active", b"pending", b"", backup.OperationFailed("daemon unavailable")):
            with self.subTest(state=state), self.assertRaises(backup.OperationFailed):
                self.docker.reset_mock()
                self.docker.side_effect = [state]
                self.drill()
            self.docker.assert_called_once_with("info", "--format", "{{.Swarm.LocalNodeState}}")

    def test_lost_create_response_cleans_only_matching_id_and_label(self):
        def lost_response(*args, **kwargs):
            if args[0] == "create":
                raise backup.OperationFailed("Docker create timed out")
            if args[0] == "inspect":
                return f"{CONTAINER} {args[-1]}".encode()
            return self.command(*args, **kwargs)
        self.docker.side_effect = lost_response
        with self.assertRaisesRegex(backup.OperationFailed, "timed out"):
            self.drill()
        self.docker.assert_called_with("rm", "-fv", CONTAINER)

    def test_bad_checksum_or_untrusted_image_aborts_before_docker(self):
        with self.assertRaises(backup.OperationFailed):
            restore_test.restore_test(self.path, "untrusted/image:latest")
        self.path.write_bytes(b"PGDMPchanged")
        with self.assertRaises(backup.OperationFailed):
            self.drill()
        self.docker.assert_not_called()
