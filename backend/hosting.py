"""Optional single-origin deployment with snapshots on the HTTP host itself."""

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from .backup import BackupError, create_backup, manifest_path
from .config import Settings
from .evaluator import Evaluator
from .main import create_app

logger = logging.getLogger(__name__)
SNAPSHOT_NAME = re.compile(r"hosted-\d{8}T\d{6}Z-[0-9a-f]{32}\.sqlite3")


def require_local_storage(database: Path) -> None:
    """SSH and HTTP may use different mounts of the same alwaysdata account."""
    database.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["stat", "-f", "-c", "%T", str(database.parent.resolve())],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    filesystem = result.stdout.strip()
    if filesystem not in {"xfs", "ext2/ext3", "btrfs", "zfs"}:
        raise RuntimeError("Hosted SQLite requires a verified local filesystem on the HTTP host")
    logger.info("Hosted SQLite filesystem verified: %s", filesystem)


class HostedSnapshots:
    def __init__(self, database: Path, directory: Path, interval: int = 21600) -> None:
        if interval < 60:
            raise ValueError("Snapshot interval must be at least 60 seconds")
        self.database, self.directory, self.interval = database, directory, interval

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = self.directory / f"hosted-{stamp}-{uuid4().hex}.sqlite3"
        create_backup(self.database, output)
        # Retain eight completed snapshots owned by this scheduler. Never prune
        # arbitrary backups, working databases, symlinks or partial file pairs.
        completed = sorted(
            (
                path
                for path in self.directory.iterdir()
                if SNAPSHOT_NAME.fullmatch(path.name)
                and path.is_file()
                and not path.is_symlink()
                and manifest_path(path).is_file()
                and not manifest_path(path).is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
        for old in completed[:-8]:
            if old.resolve().parent != self.directory.resolve():
                raise RuntimeError("Snapshot retention path escaped its directory")
            old.unlink()
            manifest_path(old).unlink()
        logger.info("Hosted SQLite snapshot verified and saved")

    async def save_safely(self) -> None:
        try:
            await asyncio.to_thread(self.save)
        except (BackupError, OSError) as exc:
            logger.error("Hosted snapshot failed (%s)", getattr(exc, "code", type(exc).__name__))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            # Keep an in-flight SQLite backup alive through graceful shutdown.
            task = asyncio.create_task(self.save_safely())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise


def create_hosted_app(*, evaluator: Evaluator | None = None) -> FastAPI:
    settings = Settings()
    frontend = Path(os.environ["CQA_FRONTEND_PATH"]).resolve(strict=True)
    if not frontend.is_dir() or not (frontend / "index.html").is_file():
        raise ValueError("CQA_FRONTEND_PATH must point to the frontend directory")
    if settings.database_path.resolve().is_relative_to(frontend):
        raise ValueError("Database must be outside the served frontend directory")
    directory = os.getenv("CQA_BACKUP_DIRECTORY", "")
    snapshots = None
    if directory:
        backup_path = Path(directory).resolve()
        if backup_path.is_relative_to(frontend):
            raise ValueError("Backups must be outside the served frontend directory")
        snapshots = HostedSnapshots(
            settings.database_path,
            backup_path,
            int(os.getenv("CQA_BACKUP_INTERVAL_SECONDS", "21600")),
        )
    app = create_app(settings, evaluator=evaluator)
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if os.getenv("CQA_REQUIRE_LOCAL_SQLITE", "0") == "1":
            await asyncio.to_thread(require_local_storage, settings.database_path)
        async with base_lifespan(application):
            task = None
            if snapshots:
                await snapshots.save_safely()
                task = asyncio.create_task(snapshots.run())
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        # The existing runtime is closed before the final snapshot.
        if snapshots:
            await snapshots.save_safely()

    app.router.lifespan_context = lifespan
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
