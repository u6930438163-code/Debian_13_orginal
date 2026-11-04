"""Offline, real-SQLite tests of safe Debian13 Work/chat/UI backup and validation."""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "state_backup.py"
spec = importlib.util.spec_from_file_location("debian13_state_backup", SCRIPT)
assert spec and spec.loader
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "work space"
        self.workspace.mkdir()
        self.snapshot = self.root / "backup-01"
        for name in backup.DATABASES:
            with sqlite3.connect(self.workspace / name) as conn:
                conn.execute("CREATE TABLE note (content TEXT NOT NULL)")
                conn.execute("INSERT INTO note(content) VALUES (?)", ("real persisted data " + name,))

    def test_all_three_databases_are_consistent_and_private(self):
        result = backup.create_backup(self.workspace, self.snapshot)
        self.assertEqual(result["status"], "created_and_verified")
        self.assertEqual(result["databases"], 3)
        self.assertFalse(result["restorePerformed"])
        self.assertFalse(result["remoteDeploymentVerified"])
        self.assertEqual(backup.verify_bundle(self.snapshot)["status"], "verified")
        self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o700)
        files = set(path.name for path in self.snapshot.iterdir())
        self.assertEqual(files, set(backup.DATABASES) | {"manifest.json"})
        for name in backup.DATABASES:
            path = self.snapshot / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with sqlite3.connect(path) as db:
                value = db.execute("SELECT content FROM note").fetchone()[0]
                self.assertEqual(value, "real persisted data " + name)

    def test_snapshot_includes_committed_wal_data(self):
        name = backup.DATABASES[0]
        with sqlite3.connect(self.workspace / name) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO note VALUES ('WAL committed before snapshot')")
            conn.commit()
            result = backup.create_backup(self.workspace, self.snapshot)
            self.assertEqual(result["status"], "created_and_verified")
            with sqlite3.connect(self.snapshot / name) as copied:
                rows = [entry[0] for entry in copied.execute("SELECT content FROM note")]
            self.assertIn("WAL committed before snapshot", rows)

    def test_existing_backup_is_never_overwritten(self):
        backup.create_backup(self.workspace, self.snapshot)
        original_hash = (self.snapshot / "manifest.json").read_bytes()
        with self.assertRaisesRegex(backup.BackupError, "already_exists"):
            backup.create_backup(self.workspace, self.snapshot)
        self.assertEqual((self.snapshot / "manifest.json").read_bytes(), original_hash)

    def test_missing_source_database_fails_closed(self):
        (self.workspace / backup.DATABASES[2]).unlink()
        with self.assertRaisesRegex(backup.BackupError, "required_database"):
            backup.create_backup(self.workspace, self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_reject_symlink_source_and_backup(self):
        outside = self.root / "target.db"
        outside.write_text("not a database")
        db = self.workspace / backup.DATABASES[0]
        db.unlink()
        db.symlink_to(outside)
        with self.assertRaisesRegex(backup.BackupError, "required_database"):
            backup.create_backup(self.workspace, self.snapshot)
        db.unlink()
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE note(content TEXT)")
        backup.create_backup(self.workspace, self.snapshot)
        self.assertEqual(backup.verify_bundle(self.snapshot)["databases"], 3)
        path = self.snapshot / backup.DATABASES[1]
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(backup.BackupError, "invalid_backup_entry"):
            backup.verify_bundle(self.snapshot)

    def test_modified_or_missing_backup_fails_verification(self):
        backup.create_backup(self.workspace, self.snapshot)
        db = self.snapshot / backup.DATABASES[0]
        with db.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(backup.BackupError, "checksum"):
            backup.verify_bundle(self.snapshot)

    def test_unexpected_extra_file_is_rejected(self):
        backup.create_backup(self.workspace, self.snapshot)
        (self.snapshot / "not-in-bundle.txt").write_text("unexpected")
        with self.assertRaisesRegex(backup.BackupError, "unexpected"):
            backup.verify_bundle(self.snapshot)

    def test_destination_must_be_outside_workspace(self):
        with self.assertRaisesRegex(backup.BackupError, "outside_workspace"):
            backup.create_backup(self.workspace, self.workspace / "backup-inside")

    def test_cli_returns_safe_json_without_path_echo(self):
        secret = "should-not-appear-in-output"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = backup.main(["--output", str(self.workspace / secret),
                                "--workspace", str(self.workspace)])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn(secret, output.getvalue())
        self.assertFalse(result["restorePerformed"])


if __name__ == "__main__":
    unittest.main()
