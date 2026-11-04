#!/usr/bin/env python3
"""Consistent local SQLite backup and read-only verification for Debian13_orginal.

Does not change the running databases, deploy source, use root or call cloud APIs.
For recovery, stop the application and restore files manually after review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DATABASES = (".debian13-work.sqlite3", "dialog_triady.sqlite3",
             ".debian13-ui-sync.sqlite3")
MANIFEST = "manifest.json"
FORMAT = "debian13-sqlite-backup-v1"


class BackupError(Exception):
    """Safe, non-secret-bearing reason for a blocked backup."""


def _regular_file(path: Path) -> bool:
    try:
        return (not path.is_symlink() and stat.S_ISREG(path.stat().st_mode))
    except OSError:
        return False


def _read_only(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_workspace(workspace: Path) -> None:
    if workspace.is_symlink() or not workspace.is_dir():
        raise BackupError("invalid_workspace")
    for name in DATABASES:
        if not _regular_file(workspace / name):
            raise BackupError("required_database_missing_or_unsafe")


def _check_integrity(path: Path) -> None:
    try:
        with _read_only(path, immutable=True) as connection:
            status = connection.execute("PRAGMA integrity_check").fetchone()
        if status != ("ok",):
            raise BackupError("backup_database_integrity_failed")
    except (sqlite3.DatabaseError, OSError, ValueError):
        raise BackupError("backup_database_integrity_failed") from None


def verify_bundle(directory: Path) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("invalid_backup_directory")
    manifest_path = directory / MANIFEST
    if not _regular_file(manifest_path) or manifest_path.stat().st_size > 8192:
        raise BackupError("invalid_backup_manifest")
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise BackupError("invalid_backup_manifest") from None
    if (not isinstance(metadata, dict)
            or metadata.get("format") != FORMAT
            or not isinstance(metadata.get("databases"), dict)
            or set(metadata["databases"]) != set(DATABASES)):
        raise BackupError("invalid_backup_manifest")
    # Reject extra files and symlinks; this directory should contain only
    # three SQLite snapshots and one manifest.
    if set(item.name for item in directory.iterdir()) != set(DATABASES) | {MANIFEST}:
        raise BackupError("unexpected_backup_contents")
    for name in DATABASES:
        path = directory / name
        expected = metadata["databases"][name]
        if (not _regular_file(path) or not isinstance(expected, dict)
                or type(expected.get("bytes")) is not int
                or expected["bytes"] <= 0
                or not isinstance(expected.get("sha256"), str)
                or len(expected["sha256"]) != 64):
            raise BackupError("invalid_backup_entry")
        try:
            if path.stat().st_size != expected["bytes"] or _sha256(path) != expected["sha256"]:
                raise BackupError("backup_checksum_mismatch")
        except OSError:
            raise BackupError("backup_checksum_mismatch") from None
        _check_integrity(path)
    return {
        "status": "verified", "format": FORMAT,
        "databases": len(DATABASES),
        "restorePerformed": False,
        "remoteDeploymentVerified": False,
    }


def create_backup(workspace: Path, destination: Path) -> dict:
    _validate_workspace(workspace)
    if destination.is_symlink() or destination.exists():
        raise BackupError("backup_destination_already_exists")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BackupError("invalid_backup_parent")
    if workspace.resolve() == parent.resolve() or workspace.resolve() in parent.resolve().parents:
        raise BackupError("backup_destination_must_be_outside_workspace")
    # SQLite online backup API snapshots committed transactions, including WAL.
    # No DB file is copied byte-by-byte from a live application.
    staging = Path(tempfile.mkdtemp(prefix=".debian13-backup-", dir=parent))
    try:
        os.chmod(staging, 0o700)
        manifest = {
            "format": FORMAT,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "databases": {},
        }
        for name in DATABASES:
            source_path = workspace / name
            if not _regular_file(source_path):
                raise BackupError("source_database_changed")
            copy = staging / name
            try:
                with _read_only(source_path) as source:
                    with sqlite3.connect(copy, timeout=10) as target:
                        source.backup(target, pages=128, sleep=0.05)
            except (sqlite3.DatabaseError, OSError):
                raise BackupError("sqlite_backup_failed") from None
            os.chmod(copy, 0o600)
            _check_integrity(copy)
            manifest["databases"][name] = {
                "sha256": _sha256(copy), "bytes": copy.stat().st_size,
            }
        meta_path = staging / MANIFEST
        meta_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(meta_path, 0o600)
        verified = verify_bundle(staging)
        if destination.exists() or destination.is_symlink():
            raise BackupError("backup_destination_already_exists")
        staging.rename(destination)
        return {**verified, "status": "created_and_verified", "backupCreated": True}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debian13 local SQLite backup/verification")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--output", type=Path, help="new private snapshot directory")
    modes.add_argument("--verify", type=Path, help="existing snapshot directory")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get(
        "WORKSPACE_DIR", str(Path.home() / ".local/share/debian13/workspace"))))
    args = parser.parse_args(argv)
    previous = os.umask(0o077)
    try:
        result = (verify_bundle(args.verify) if args.verify is not None
                  else create_backup(args.workspace, args.output))
    except (BackupError, OSError) as error:
        # OSError strings can expose filesystem paths. Print only fixed categories.
        reason = str(error) if isinstance(error, BackupError) else "filesystem_unavailable"
        result = {"status": "blocked", "reason": reason,
                  "restorePerformed": False, "remoteDeploymentVerified": False}
        code = 1
    else:
        code = 0
    finally:
        os.umask(previous)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
