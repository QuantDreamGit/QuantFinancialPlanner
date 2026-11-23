"""Coordinated SQLite backups of a ledger and all its workspaces."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

try:
    import fcntl
except ImportError:
    fcntl = None


STAMP = "%Y%m%dT%H%M%S%fZ"
SNAPSHOT_NAME = re.compile(r"quant-[0-9]{8}T[0-9]{12}Z")


@contextmanager
def ledger_access(database, *, exclusive=False):
    """Let API requests run together while snapshots take an exclusive lock."""
    if fcntl is None:
        if exclusive:
            raise OSError("Quant backups require POSIX file locking.")
        yield
        return
    database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(database.parent / ("." + database.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "rb"):
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def prior_snapshots(directory, database):
    if not directory.exists():
        return []
    snapshots = []
    for folder in directory.iterdir():
        if not SNAPSHOT_NAME.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
            continue
        try:
            stamp = datetime.strptime(folder.name.removeprefix("quant-"), STAMP).replace(tzinfo=timezone.utc)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and manifest.get("format") == 1 and manifest.get("source") == str(database):
                snapshots.append((stamp, folder))
        except (OSError, ValueError):
            continue
    return sorted(snapshots)


def copy_sqlite(original, target):
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")
            if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError(f"SQLite integrity check failed for {original.name}.")


def file_record(file, folder):
    checksum = hashlib.sha256()
    with file.open("rb") as contents:
        for chunk in iter(lambda: contents.read(1024 * 1024), b""):
            checksum.update(chunk)
    return {"path": file.relative_to(folder).as_posix(), "bytes": file.stat().st_size, "sha256": checksum.hexdigest()}


def backup_due(snapshots, now, options):
    return not snapshots or (now - snapshots[-1][0]).total_seconds() >= options.interval_hours * 3600


def create_backup(database, options, *, due_only=False):
    """Publish a backup only after all of its files have been verified."""
    if not database.is_file():
        raise OSError(f"The SQLite ledger does not exist at {database}.")
    with ledger_access(database, exclusive=True):
        now = datetime.now(timezone.utc)
        previous = prior_snapshots(options.directory, database)
        if due_only and not backup_due(previous, now, options):
            return None
        options.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination = options.directory / ("quant-" + now.strftime(STAMP))
        with TemporaryDirectory(dir=options.directory, prefix=".quant-staging-") as staging:
            folder = Path(staging) / "snapshot"
            folder.mkdir(mode=0o700)
            payload = folder / "ledger"
            payload.mkdir(mode=0o700)
            workspace = database.with_suffix(".workspaces")
            databases = [database, *sorted(workspace.glob("*.sqlite3"))]
            keys = sorted(workspace.glob("*-fernet.key"))
            files = []
            for original in databases + keys:
                target = payload / original.relative_to(database.parent)
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if original in keys:
                    shutil.copyfile(original, target)
                else:
                    copy_sqlite(original, target)
                target.chmod(0o600)
                files.append(file_record(target, folder))
            manifest = {"format": 1, "created_at": now.isoformat(), "source": str(database), "files": files}
            metadata = folder / "manifest.json"
            metadata.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            metadata.chmod(0o600)
            folder.rename(destination)
        for _, old in previous[:max(0, len(previous) + 1 - options.keep)]:
            shutil.rmtree(old)
        return destination


class BackupWorker(Thread):
    def __init__(self, database, options):
        super().__init__(daemon=True, name="quant-backups")
        self.database = database
        self.options = options
        self.stopped = Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                snapshots = prior_snapshots(self.options.directory, self.database)
                if backup_due(snapshots, datetime.now(timezone.utc), self.options):
                    result = create_backup(self.database, self.options, due_only=True)
                    if result is not None:
                        print(f"Backup saved at {result}", flush=True)
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f"Quant backup failed: {error}", file=sys.stderr, flush=True)
            self.stopped.wait(60)

    def finish(self):
        self.stopped.set()
        self.join(timeout=2)
