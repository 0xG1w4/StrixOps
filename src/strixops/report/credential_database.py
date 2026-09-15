"""Disk-backed credential transactions with read-only, non-creating readers."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import quote

DATABASE_NAME = "credentials.sqlite3"
SCHEMA_VERSION = 1
_TABLES = {
    "credentials": (
        "CREATE TABLE credentials (id TEXT PRIMARY KEY, host TEXT NOT NULL, username TEXT NOT NULL, "
        "secret_type TEXT NOT NULL, validation_status TEXT NOT NULL, severity TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, validation_rank INTEGER NOT NULL, severity_rank INTEGER NOT NULL, "
        "search_text TEXT NOT NULL, record TEXT NOT NULL)"
    ),
    "credential_datasets": (
        "CREATE TABLE credential_datasets "
        "(id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, record TEXT NOT NULL)"
    ),
}
_INDEXES = {
    "credentials_updated": "CREATE INDEX credentials_updated ON credentials (updated_at DESC, id)",
    "credentials_report": (
        "CREATE INDEX credentials_report ON credentials "
        "(validation_rank DESC, severity_rank DESC, updated_at DESC, id)"
    ),
    "credentials_status": "CREATE INDEX credentials_status ON credentials (validation_status)",
    "credentials_type": "CREATE INDEX credentials_type ON credentials (secret_type)",
    "credential_datasets_path": "CREATE INDEX credential_datasets_path ON credential_datasets (path)",
}


class DatabaseError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Credential database operation failed.")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class CredentialDatabase:
    """SQLite owns row transactions; a file lock serializes initial migration.

    DELETE journaling keeps readers strictly read-only: opening a reader cannot
    create WAL/SHM files. Long CSV readers hold one SQLite read snapshot. Writers
    wait for that reader only when committing, rather than loading its rows.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir).absolute()

    @contextmanager
    def directory(self, *, create: bool = False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(self.run_dir, flags)
        directory = None
        try:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(".state", mode=0o700, dir_fd=root)
            with suppress(FileNotFoundError):
                directory = os.open(".state", flags, dir_fd=root)
            yield directory
        finally:
            if directory is not None:
                os.close(directory)
            os.close(root)

    @staticmethod
    def safe_file(directory: int, name: str) -> os.stat_result | None:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DatabaseError("unsafe_storage")
        return info

    def _assert_directory(self, directory: int) -> None:
        with self.directory() as current:
            if current is None or _identity(os.fstat(current)) != _identity(os.fstat(directory)):
                raise DatabaseError("storage_unavailable")

    def _check_files(self, directory: int, name: str) -> os.stat_result | None:
        self._assert_directory(directory)
        info = self.safe_file(directory, name)
        for suffix in ("-journal", "-wal", "-shm"):
            self.safe_file(directory, name + suffix)
        self.safe_file(directory, "credentials.lock")
        return info

    @contextmanager
    def _writer_lock(self, directory: int, cancelled=None):
        self.safe_file(directory, "credentials.lock")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open("credentials.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open("credentials.lock", flags, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatabaseError("unsafe_storage")
            deadline = time.monotonic() + 30
            while True:
                if cancelled is not None and cancelled():
                    raise DatabaseError("cancelled")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DatabaseError("storage_unavailable") from None
                    time.sleep(0.01)
            current = self.safe_file(directory, "credentials.lock")
            if current is None or _identity(current) != _identity(info):
                raise DatabaseError("storage_unavailable")
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _schema(connection: sqlite3.Connection, *, initialize: bool = False) -> None:
        if initialize:
            for statement in (*_TABLES.values(), *_INDEXES.values()):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise DatabaseError("invalid_store")
        objects = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if objects != {**_TABLES, **_INDEXES}:
            raise DatabaseError("invalid_store")

    @contextmanager
    def _connection(self, directory: int, *, write: bool):
        original = self._check_files(directory, DATABASE_NAME)
        if original is None and not write:
            yield None, False, directory
            return
        fresh = original is None
        name = f".credentials-{uuid.uuid4().hex}.sqlite3" if fresh else DATABASE_NAME
        connection = None
        published = False
        try:
            if fresh:
                descriptor = os.open(
                    name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
                os.close(descriptor)
            expected = self._check_files(directory, name)
            if expected is None:
                raise DatabaseError("storage_unavailable")
            path = quote(str(self.run_dir / ".state" / name), safe="/")
            mode = "rw" if write else "ro"
            connection = sqlite3.connect(
                f"file:{path}?mode={mode}",
                uri=True,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            current = self._check_files(directory, name)
            if current is None or _identity(current) != _identity(expected):
                raise DatabaseError("storage_unavailable")
            connection.execute("PRAGMA trusted_schema=OFF")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            elif fresh:
                connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._schema(connection, initialize=fresh)
            yield connection, fresh, directory
            current = self._check_files(directory, name)
            if current is None or _identity(current) != _identity(expected):
                raise DatabaseError("storage_unavailable")
            if write:
                connection.commit()
            else:
                connection.rollback()
            connection.close()
            connection = None
            if fresh:
                if self._check_files(directory, DATABASE_NAME) is not None:
                    raise DatabaseError("storage_unavailable")
                os.replace(name, DATABASE_NAME, src_dir_fd=directory, dst_dir_fd=directory)
                published = True
                with suppress(OSError):
                    os.fsync(directory)
        finally:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.rollback()
                connection.close()
            if fresh and not published:
                for suffix in ("", "-journal", "-wal", "-shm"):
                    with suppress(OSError):
                        os.unlink(name + suffix, dir_fd=directory)

    @contextmanager
    def transaction(self, *, write: bool = False, cancelled=None):
        """Yield (connection | None, new_database, anchored_state_directory)."""
        try:
            with self.directory(create=write) as directory:
                if directory is None:
                    yield None, False, None
                elif write:
                    with (
                        self._writer_lock(directory, cancelled),
                        self._connection(directory, write=True) as session,
                    ):
                        yield session
                else:
                    with self._connection(directory, write=False) as session:
                        yield session
        except sqlite3.DatabaseError as exc:
            # Never expose provider/SQLite messages containing paths or records.
            code = "storage_unavailable" if isinstance(exc, sqlite3.OperationalError) else "invalid_store"
            raise DatabaseError(code) from None
