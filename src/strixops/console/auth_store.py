"""One local Console account, private password/session storage and bounded login work.

The built-in password blocklist is intentionally small; it is not a complete
compromised-password database. Callers supply the transport's actual client IP,
never an unvalidated forwarding header. Public dictionaries contain no hashes.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USERNAME = "strix"
DEFAULT_PASSWORD = "strix123"
SCRYPT_N = 1 << 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
SESSION_ABSOLUTE_SECONDS = 12 * 60 * 60
SESSION_IDLE_SECONDS = 30 * 60
MAX_SESSIONS = 10
RATE_WINDOW_SECONDS = 300
RATE_IP_ATTEMPTS = 10
RATE_GLOBAL_ATTEMPTS = 50
MAX_RATE_BUCKETS = 2048
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_KDF_SLOTS = threading.BoundedSemaphore(2)
_STORE_LOCK = threading.Lock()
_STORES: dict[str, AuthStore] = {}
_BLOCKLIST = frozenset(
    {
        "password",
        "password123",
        "passwordpassword",
        "password123456789",
        "password1234567890",
        "letmein",
        "letmeinletmeinletmein",
        "welcome",
        "welcomewelcome123",
        "administrator",
        "administrator123",
        "thisisapassword",
        "changeme",
        "changemepassword",
        "correcthorsebatterystaple",
        "iloveyou",
        "iloveyouiloveyou",
        "qwerty",
        "strix123",
    }
)
_MESSAGES = {
    "invalid_credentials": "The username or password is incorrect.",
    "current_password_invalid": "The current password is incorrect.",
    "password_reused": "Choose a password different from the current password.",
    "weak_password": (
        "Use 8–32 characters and avoid common, repeated, sequential or account-derived passwords."
    ),
    "invalid_session": "Your session has expired. Sign in again.",
    "rate_limited": "Too many authentication attempts. Try again later.",
    "auth_busy": "Authentication is busy. Try again shortly.",
    "auth_unavailable": "The account store is unavailable. Existing account data has been preserved.",
}
_COLUMNS = {
    "account": {
        "id",
        "username",
        "salt",
        "password_hash",
        "must_change",
        "generation",
        "created_at",
        "password_changed_at",
    },
    "sessions": {"token_hash", "csrf_hash", "created_at", "last_seen", "expires_at", "generation"},
    "login_history": {"id", "at", "ip"},
    "rate_limits": {"bucket", "window_start", "attempts"},
}


class AuthError(RuntimeError):
    def __init__(self, code: str, status: int = 400, retry_after: int | None = None):
        self.code = code if code in _MESSAGES else "auth_unavailable"
        self.status = status
        self.retry_after = retry_after
        super().__init__(_MESSAGES[self.code])


def default_auth_path() -> Path:
    override = (os.environ.get("STRIXOPS_AUTH_DB") or "").strip()
    if override:
        return Path(override).expanduser().absolute()
    from strixops.console.settings_store import config_path

    return config_path().expanduser().absolute().parent / "auth.sqlite3"


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _ip(value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 64:
            return "unknown"
        return str(ipaddress.ip_address(value))
    except ValueError:
        return "unknown"


def _password_bytes(value: Any) -> bytes | None:
    # Keep the previous login range so existing passwords can still be changed.
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        normalized = unicodedata.normalize("NFKC", value)
        encoded = normalized.encode("utf-8")
    except (UnicodeError, ValueError):
        return None
    return encoded if len(normalized) <= 128 and len(encoded) <= 512 else None


def validate_new_password(value: Any) -> bytes:
    encoded = _password_bytes(value)
    if encoded is None:
        raise AuthError("weak_password")
    normalized = encoded.decode("utf-8")
    if not 8 <= len(normalized) <= 32 or any(
        unicodedata.category(char).startswith("C") for char in normalized
    ):
        raise AuthError("weak_password")
    compact = "".join(char for char in normalized.casefold() if char.isalnum())
    repeated = "".join(char for char in normalized.casefold() if not char.isspace())
    if not repeated or compact in _BLOCKLIST or USERNAME in compact:
        raise AuthError("weak_password")
    if any(
        len(repeated) % length == 0 and repeated == repeated[:length] * (len(repeated) // length)
        for length in range(1, len(repeated) // 2 + 1)
    ):
        raise AuthError("weak_password")
    for sequence in (
        "0123456789",
        "1234567890",
        "abcdefghijklmnopqrstuvwxyz",
        "qwertyuiop",
        "asdfghjkl",
        "zxcvbnm",
    ):
        if compact and any(
            compact in direction * (len(compact) // len(direction) + 2)
            for direction in (sequence, sequence[::-1])
        ):
            raise AuthError("weak_password")
    return encoded


def _token_hash(token: Any) -> bytes | None:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


def _csrf_token(token: str) -> str:
    digest = hashlib.sha256(b"strixops-csrf-v1:" + token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class AuthStore:
    def __init__(self, path: Path | str | None = None, *, clock: Callable[[], float] = time.time):
        self.path = (Path(path) if path is not None else default_auth_path()).expanduser().absolute()
        self._clock = clock
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._file_lock("init", blocking=True):
                if not self._checked_file(self.path, missing=True):
                    self._initialize()
                with self._db() as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise AuthError("auth_unavailable", 503)
                    self._account_row(db)
        except AuthError:
            raise
        except (OSError, ValueError, sqlite3.Error, OverflowError) as exc:
            raise AuthError("auth_unavailable", 503) from exc

    def _checked_file(self, path: Path, *, missing: bool = False) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return False
            raise
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise AuthError("auth_unavailable", 503)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise AuthError("auth_unavailable", 503)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return True

    @contextlib.contextmanager
    def _file_lock(self, suffix: str, *, blocking: bool = False) -> Iterator[None]:
        path = self.path.with_name(self.path.name + f".{suffix}.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise AuthError("auth_unavailable", 503)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def _derive(self, password: bytes, salt: bytes) -> bytes:
        if not _KDF_SLOTS.acquire(blocking=False):
            raise AuthError("auth_busy", 503, 1)
        try:
            # File slots also bound KDF work across Console worker processes.
            for slot in range(2):
                try:
                    with self._file_lock(f"kdf{slot}"):
                        return hashlib.scrypt(
                            password,
                            salt=salt,
                            n=SCRYPT_N,
                            r=SCRYPT_R,
                            p=SCRYPT_P,
                            maxmem=SCRYPT_MAXMEM,
                            dklen=64,
                        )
                except BlockingIOError:
                    continue
            raise AuthError("auth_busy", 503, 1)
        except AuthError:
            raise
        except (OSError, ValueError) as exc:
            raise AuthError("auth_unavailable", 503) from exc
        finally:
            _KDF_SLOTS.release()

    def _initialize(self) -> None:
        # An existing DB, including a zero-byte or corrupt DB, is never replaced.
        salt = secrets.token_bytes(32)
        password_hash = self._derive(DEFAULT_PASSWORD.encode(), salt)
        fd, temporary = tempfile.mkstemp(prefix=".auth-init-", dir=self.path.parent)
        os.close(fd)
        try:
            db = sqlite3.connect(temporary)
            try:
                db.executescript("""
                    CREATE TABLE account(
                      id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL CHECK(username='strix'),
                      salt BLOB NOT NULL CHECK(length(salt)=32),
                      password_hash BLOB NOT NULL CHECK(length(password_hash)=64),
                      must_change INTEGER NOT NULL CHECK(must_change IN (0,1)), generation INTEGER NOT NULL,
                      created_at REAL NOT NULL, password_changed_at REAL);
                    CREATE TABLE sessions(
                      token_hash BLOB PRIMARY KEY CHECK(length(token_hash)=32),
                      csrf_hash BLOB NOT NULL CHECK(length(csrf_hash)=32),
                      created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL,
                      generation INTEGER NOT NULL);
                    CREATE TABLE login_history(
                      id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, ip TEXT NOT NULL);
                    CREATE TABLE rate_limits(
                      bucket TEXT PRIMARY KEY, window_start REAL NOT NULL, attempts INTEGER NOT NULL);
                    PRAGMA user_version=1;
                """)
                db.execute(
                    "INSERT INTO account VALUES(1,?,?,?,?,1,?,NULL)",
                    (USERNAME, salt, password_hash, 1, self._clock()),
                )
                db.commit()
            finally:
                db.close()
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(temporary, self.path)  # atomic publication, never overwrites an existing path
            os.unlink(temporary)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    @contextlib.contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            self._checked_file(self.path)
            for suffix in ("-wal", "-shm", "-journal"):
                self._checked_file(Path(str(self.path) + suffix), missing=True)
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise AuthError("auth_unavailable", 503)
            for table, columns in _COLUMNS.items():
                if not columns.issubset({row[1] for row in db.execute(f"PRAGMA table_info({table})")}):
                    raise AuthError("auth_unavailable", 503)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except AuthError:
            if db is not None:
                db.rollback()
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error, OverflowError) as exc:
            if db is not None:
                db.rollback()
            raise AuthError("auth_unavailable", 503) from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _account_row(db: sqlite3.Connection) -> sqlite3.Row:
        rows = db.execute("SELECT * FROM account").fetchall()
        if len(rows) != 1:
            raise AuthError("auth_unavailable", 503)
        row = rows[0]
        if (
            row["id"] != 1
            or row["username"] != USERNAME
            or not isinstance(row["salt"], bytes)
            or len(row["salt"]) != 32
            or not isinstance(row["password_hash"], bytes)
            or len(row["password_hash"]) != 64
            or row["must_change"] not in (0, 1)
            or type(row["generation"]) is not int
            or row["generation"] < 1
        ):
            raise AuthError("auth_unavailable", 503)
        return row

    def _rate_limit(self, ip: str) -> None:
        now = self._clock()
        ip_key = "ip:" + hashlib.sha256(ip.encode()).hexdigest()
        with self._db(write=True) as db:
            db.execute("DELETE FROM rate_limits WHERE window_start <= ?", (now - RATE_WINDOW_SECONDS,))
            entries = []
            missing = 0
            for bucket, maximum in (("global", RATE_GLOBAL_ATTEMPTS), (ip_key, RATE_IP_ATTEMPTS)):
                row = db.execute(
                    "SELECT window_start,attempts FROM rate_limits WHERE bucket=?", (bucket,)
                ).fetchone()
                start, count = (row[0], row[1]) if row else (now, 0)
                missing += int(row is None)
                if (
                    not isinstance(start, (int, float))
                    or not math.isfinite(start)
                    or type(count) is not int
                    or count < 0
                ):
                    raise AuthError("auth_unavailable", 503)
                if count >= maximum:
                    raise AuthError("rate_limited", 429, max(1, math.ceil(start + RATE_WINDOW_SECONDS - now)))
                entries.append((bucket, start, count + 1))
            # This cap is independent of distinct attacker-controlled source IPs.
            if db.execute("SELECT count(*) FROM rate_limits").fetchone()[0] + missing > MAX_RATE_BUCKETS:
                oldest = db.execute("SELECT min(window_start) FROM rate_limits").fetchone()[0]
                raise AuthError("rate_limited", 429, max(1, math.ceil(oldest + RATE_WINDOW_SECONDS - now)))
            db.executemany(
                "INSERT INTO rate_limits VALUES(?,?,?) ON CONFLICT(bucket) DO UPDATE SET "
                "window_start=excluded.window_start,attempts=excluded.attempts",
                entries,
            )

    @staticmethod
    def _last_login(db: sqlite3.Connection) -> dict | None:
        row = db.execute("SELECT at,ip FROM login_history ORDER BY id DESC LIMIT 1").fetchone()
        return {"at": _timestamp(row[0]), "ip": row[1]} if row else None

    def _public_session(
        self, db: sqlite3.Connection, row: sqlite3.Row, account: sqlite3.Row, token: str
    ) -> dict:
        return {
            "username": USERNAME,
            "must_change_password": bool(account["must_change"]),
            "created_at": _timestamp(row["created_at"]),
            "expires_at": _timestamp(row["expires_at"]),
            "idle_expires_at": _timestamp(min(row["expires_at"], row["last_seen"] + SESSION_IDLE_SECONDS)),
            "csrf_token": _csrf_token(token),
            "last_login": self._last_login(db),
        }

    def _session_row(
        self, db: sqlite3.Connection, token_hash: bytes, account: sqlite3.Row
    ) -> sqlite3.Row | None:
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        now = self._clock()
        if row is None:
            return None
        if any(
            not isinstance(row[field], (int, float)) or not math.isfinite(row[field])
            for field in ("created_at", "last_seen", "expires_at")
        ):
            raise AuthError("auth_unavailable", 503)
        if (
            row["generation"] != account["generation"]
            or now >= row["expires_at"]
            or now >= row["last_seen"] + SESSION_IDLE_SECONDS
        ):
            return None
        return row

    def _issue_session(self, db: sqlite3.Connection, account: sqlite3.Row) -> dict:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        csrf = _csrf_token(token)
        db.execute(
            "DELETE FROM sessions WHERE expires_at<=? OR last_seen<=? OR generation!=?",
            (now, now - SESSION_IDLE_SECONDS, account["generation"]),
        )
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
            (
                _token_hash(token),
                _token_hash(csrf),
                now,
                now,
                now + SESSION_ABSOLUTE_SECONDS,
                account["generation"],
            ),
        )
        db.execute(
            "DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions "
            "ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
            (MAX_SESSIONS,),
        )
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)).fetchone()
        return {"token": token, "csrf_token": csrf, "session": self._public_session(db, row, account, token)}

    def login(self, username: Any, password: Any, ip: str) -> dict:
        ip = _ip(ip)
        self._rate_limit(ip)
        candidate = _password_bytes(password)
        if candidate is None:
            raise AuthError("invalid_credentials", 401)
        with self._db() as db:
            account = self._account_row(db)
        derived = self._derive(candidate, account["salt"])
        if not hmac.compare_digest(derived, account["password_hash"]) or username != USERNAME:
            raise AuthError("invalid_credentials", 401)
        with self._db(write=True) as db:
            current = self._account_row(db)
            if current["generation"] != account["generation"]:
                raise AuthError("invalid_credentials", 401)
            db.execute("INSERT INTO login_history(at,ip) VALUES(?,?)", (self._clock(), ip))
            db.execute(
                "DELETE FROM login_history WHERE id NOT IN "
                "(SELECT id FROM login_history ORDER BY id DESC LIMIT 5)"
            )
            return self._issue_session(db, current)

    def authenticate(self, token: Any, touch: bool = True) -> dict | None:
        digest = _token_hash(token)
        if digest is None:
            return None
        with self._db(write=touch) as db:
            account = self._account_row(db)
            row = self._session_row(db, digest, account)
            if row is None:
                if touch:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                return None
            if touch:
                db.execute(
                    "UPDATE sessions SET last_seen=max(last_seen,?) WHERE token_hash=?",
                    (self._clock(), digest),
                )
                row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (digest,)).fetchone()
            return self._public_session(db, row, account, token)

    def validate_csrf(self, token: Any, csrf_token: Any) -> bool:
        digest, csrf = _token_hash(token), _token_hash(csrf_token)
        if digest is None or csrf is None:
            return False
        with self._db() as db:
            row = self._session_row(db, digest, self._account_row(db))
            return (
                row is not None
                and isinstance(row["csrf_hash"], bytes)
                and hmac.compare_digest(row["csrf_hash"], csrf)
            )

    def change_password(self, token: Any, current_password: Any, new_password: Any, ip: str) -> dict:
        digest = _token_hash(token)
        if digest is None:
            raise AuthError("invalid_session", 401)
        self._rate_limit(_ip(ip))
        with self._db() as db:
            account = self._account_row(db)
            if self._session_row(db, digest, account) is None:
                raise AuthError("invalid_session", 401)
        current = _password_bytes(current_password)
        if current is None or not hmac.compare_digest(
            self._derive(current, account["salt"]), account["password_hash"]
        ):
            raise AuthError("current_password_invalid", 400)
        new = _password_bytes(new_password)
        if new is not None and hmac.compare_digest(current, new):
            raise AuthError("password_reused")
        new = validate_new_password(new_password)
        salt = secrets.token_bytes(32)
        derived = self._derive(new, salt)
        with self._db(write=True) as db:
            latest = self._account_row(db)
            if latest["generation"] != account["generation"] or self._session_row(db, digest, latest) is None:
                raise AuthError("invalid_session", 401)
            db.execute(
                "UPDATE account SET salt=?,password_hash=?,must_change=0,generation=generation+1,"
                "password_changed_at=? WHERE id=1",
                (salt, derived, self._clock()),
            )
            db.execute("DELETE FROM sessions")
            return self._issue_session(db, self._account_row(db))

    def logout(self, token: Any) -> None:
        digest = _token_hash(token)
        if digest is not None:
            with self._db(write=True) as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))

    def account(self) -> dict:
        with self._db() as db:
            row = self._account_row(db)
            return {
                "username": USERNAME,
                "must_change_password": bool(row["must_change"]),
                "password_changed_at": _timestamp(row["password_changed_at"])
                if row["password_changed_at"] is not None
                else None,
            }

    def history(self) -> list[dict]:
        with self._db() as db:
            return [
                {"at": _timestamp(row[0]), "ip": row[1]}
                for row in db.execute("SELECT at,ip FROM login_history ORDER BY id DESC LIMIT 5")
            ]


def get_store() -> AuthStore:
    path = default_auth_path()
    with _STORE_LOCK:
        key = str(path)
        if key not in _STORES:
            _STORES[key] = AuthStore(path)
        return _STORES[key]
