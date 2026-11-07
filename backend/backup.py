"""Offline administration CLI; importing this module never starts an AI worker."""

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from .clock import utc_now
from .db import Repository
from .models import (
    AppealRecord,
    ChecklistDefinition,
    Contract,
    Conversation,
    DatasetInfo,
    EvaluationRecord,
)
from .work_models import BatchRecord, ProviderUsage
from .work_store import WorkStore

TABLES = (
    "datasets",
    "conversations",
    "checklists",
    "checklist_versions",
    "evaluations",
    "appeals",
    "execution_owners",
    "evaluation_batches",
    "batch_creation_requests",
    "batch_items",
    "evaluation_executions",
)
SIDECARS = ("-wal", "-shm", "-journal")
MAX_MANIFEST_BYTES = 64 * 1024


class BackupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackupManifest(Contract):
    format_version: Literal[1] = 1
    created_at: datetime
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(strict=True, gt=0)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def manifest_path(backup: Path) -> Path:
    return backup.with_name(backup.name + ".manifest.json")


def _exists(path: Path) -> bool:
    # Unlike Path.exists(), this also rejects a dangling destination symlink.
    return os.path.lexists(path)


def _source(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise BackupError("INVALID_PATH", "Source must be an existing regular file")
    return resolved


def _destination(path: Path) -> Path:
    if os.name == "nt" and (":" in path.name or path.is_reserved()):
        raise BackupError(
            "INVALID_PATH", "Destination must be a regular filename, not a Windows device or stream"
        )
    if _exists(path):
        raise BackupError("DESTINATION_EXISTS", "Destination already exists; choose a new file")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.name in {"", ".", ".."}:
        raise BackupError("INVALID_PATH", "Destination requires an existing directory and filename")
    resolved = parent / path.name
    if _exists(resolved) or any(_exists(Path(str(resolved) + suffix)) for suffix in SIDECARS):
        raise BackupError("DESTINATION_EXISTS", "Destination or SQLite sidecar already exists")
    return resolved


@contextmanager
def _stage(parent: Path | None = None) -> Iterator[Path]:
    fd, name = tempfile.mkstemp(prefix=".cqa-backup-", suffix=".sqlite3", dir=parent)
    os.close(fd)
    path = Path(name).resolve()
    try:
        yield path
    finally:
        # Only these exact, privately created paths can be removed; never the source.
        for candidate in [path, *(Path(str(path) + suffix) for suffix in SIDECARS)]:
            candidate.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync(path: Path) -> None:
    with path.open("rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _publish(stage: Path, destination: Path) -> None:
    try:
        # Same-filesystem hard links atomically publish a finished file without
        # replacing an existing name (unlike os.replace / POSIX rename).
        os.link(stage, destination)
    except FileExistsError:
        raise BackupError(
            "DESTINATION_EXISTS", "Destination was created by another operation"
        ) from None


@contextmanager
def _read_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    # immutable is safe ONLY for the private, closed copy, never a live WAL database.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        yield conn


def _inspect(path: Path) -> dict[str, int]:
    with _read_snapshot(path) as conn:
        if [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise BackupError("INTEGRITY_FAILED", "SQLite integrity_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("FOREIGN_KEYS_FAILED", "SQLite foreign_key_check failed")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES).issubset(tables):
            raise BackupError("SCHEMA_UNSUPPORTED", "Database is missing current backend tables")
        # Validate stored contracts, not just SQL pages; never emit stored content.
        for row in conn.execute("SELECT id,name,group_id,created_at FROM datasets"):
            DatasetInfo.model_validate(dict(row))
        json_tables: tuple[tuple[str, type[BaseModel]], ...] = (
            ("conversations", Conversation),
            ("checklist_versions", ChecklistDefinition),
            ("appeals", AppealRecord),
        )
        for table, model in json_tables:
            for row in conn.execute(f"SELECT body FROM {table}"):
                model.model_validate_json(row[0])
        for row in conn.execute("SELECT id FROM evaluations"):
            Repository._get_evaluation(conn, row[0])
        for row in conn.execute("SELECT id FROM evaluation_batches"):
            WorkStore._batch(conn, row[0])
        for row in conn.execute("SELECT response FROM batch_creation_requests"):
            BatchRecord.model_validate_json(row[0])
        for row in conn.execute("SELECT response,usage FROM evaluation_executions"):
            if row[0] is not None:
                EvaluationRecord.model_validate_json(row[0])
            if row[1] is not None:
                ProviderUsage.model_validate_json(row[1])
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
        }


def _online_copy(source: Path, destination: Path, timeout: float) -> None:
    deadline = monotonic() + timeout

    def progress(status: int, remaining: int, total: int) -> None:
        if monotonic() >= deadline:
            raise BackupError("BACKUP_TIMEOUT", "Snapshot timed out; no backup was published")

    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=timeout)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst, pages=128, progress=progress, sleep=0.02)
        # The output is a standalone artifact; all WAL data is incorporated by backup().
        if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise BackupError("SNAPSHOT_FAILED", "Could not finalize standalone SQLite snapshot")


@contextmanager
def _operation() -> Iterator[None]:
    try:
        yield
    except ValidationError:
        raise BackupError(
            "INVALID_CONTENT", "Stored metadata or backend records are invalid"
        ) from None
    except sqlite3.Error:
        raise BackupError(
            "DATABASE_ERROR", "SQLite could not read or validate the snapshot"
        ) from None
    except OSError as exc:
        # Filenames/record contents and SQL errors do not belong in diagnostic output.
        raise BackupError(
            "FILESYSTEM_ERROR", f"Filesystem operation failed ({type(exc).__name__})"
        ) from None


def create_backup(database: Path, output: Path, *, timeout: float = 60) -> BackupManifest:
    """Publish a validated SQLite snapshot plus its manifest, without stopping writers."""
    with _operation():
        if not math.isfinite(timeout) or timeout <= 0:
            raise BackupError("INVALID_TIMEOUT", "Timeout must be a positive finite number")
        source, destination = _source(database), _destination(output)
        metadata_path = _destination(manifest_path(destination))
        with _stage(destination.parent) as stage, _stage(destination.parent) as metadata_stage:
            _online_copy(source, stage, timeout)
            _inspect(stage)
            manifest = BackupManifest(
                created_at=datetime.fromisoformat(utc_now()),
                file_name=destination.name,
                size_bytes=stage.stat().st_size,
                sha256=_digest(stage),
            )
            metadata_stage.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            _sync(stage)
            _sync(metadata_stage)
            _publish(stage, destination)
            try:
                _publish(metadata_stage, metadata_path)
            except BaseException:
                # Roll back only our own link, preserving a concurrently replaced file.
                if _exists(destination) and destination.samefile(stage):
                    destination.unlink()
                raise
            return manifest


def _verified_copy(backup: Path, stage: Path) -> dict[str, int]:
    source = _source(backup)
    if any(_exists(Path(str(source) + suffix)) for suffix in SIDECARS):
        raise BackupError(
            "BACKUP_IN_USE", "Backup has SQLite sidecars; use an immutable backup artifact"
        )
    with manifest_path(source).open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BackupError("INVALID_MANIFEST", "Backup manifest exceeds size limit")
    manifest = BackupManifest.model_validate_json(raw)
    if manifest.file_name != source.name:
        raise BackupError("INVALID_MANIFEST", "Manifest filename does not match backup")
    shutil.copyfile(source, stage)
    if stage.stat().st_size != manifest.size_bytes or _digest(stage) != manifest.sha256:
        raise BackupError("HASH_MISMATCH", "Backup size or SHA-256 does not match manifest")
    return _inspect(stage)


def verify_backup(backup: Path) -> dict[str, int]:
    """Verify an isolated copy. No migrations, worker startup, or database changes."""
    with _operation(), _stage() as stage:
        return _verified_copy(backup, stage)


def _suspend_unfinished(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        executions = conn.execute(
            "UPDATE evaluation_executions SET status='interrupted',"
            "error_code='RESTORE_REQUIRES_REVIEW' WHERE status='running'"
        ).rowcount
        items = conn.execute(
            "UPDATE batch_items SET status='interrupted',error_code='RESTORE_REQUIRES_REVIEW' "
            "WHERE status IN ('pending','running')"
        ).rowcount
        conn.execute("UPDATE execution_owners SET expires_at=0")
    return {"executions": executions, "batch_items": items}


def restore_backup(backup: Path, output: Path) -> dict[str, dict[str, int]]:
    """Restore into a new file; suppress replay even if a pending item ran after backup."""
    with _operation():
        destination = _destination(output)
        with _stage(destination.parent) as stage:
            _verified_copy(backup, stage)
            suspended = _suspend_unfinished(stage)
            counts = _inspect(stage)
            _sync(stage)
            _publish(stage, destination)
            return {"records": counts, "suspended": suspended}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create, verify or restore a CQA SQLite backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Online WAL-safe backup into new files")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--timeout", type=float, default=60, help="Snapshot deadline in seconds")
    verify = commands.add_parser("verify", help="Verify snapshot without starting the application")
    verify.add_argument("--backup", type=Path, required=True)
    restore = commands.add_parser(
        "restore", help="Restore to NEW file; suspend all unfinished work"
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_backup(args.database, args.output, timeout=args.timeout).model_dump(
                mode="json"
            )
        elif args.command == "verify":
            result = {"records": verify_backup(args.backup)}
        else:
            result = restore_backup(args.backup, args.output)
    except BackupError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
