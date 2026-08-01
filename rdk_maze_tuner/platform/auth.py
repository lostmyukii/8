"""Argon2 accounts, opaque server-side sessions, CSRF, and login limiting."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .audit_log import AuditLog
from .database import Database


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1_024
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60


class AuthenticationError(RuntimeError):
    """Raised for an invalid, expired, or revoked login."""


class UsernamePolicyError(ValueError):
    """Raised when a username is unsafe or unsupported."""


class UsernameExistsError(RuntimeError):
    """Raised when a normalized username already exists."""


class PasswordPolicyError(ValueError):
    """Raised when a password does not meet the local policy."""


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many login failures")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    status: str


@dataclass(frozen=True)
class SessionPrincipal:
    user_id: str
    username: str
    session_id: str
    expires_at: datetime


@dataclass(frozen=True)
class SessionCredentials:
    session_token: str
    csrf_token: str
    expires_at: datetime
    user: User


class AuthService:
    def __init__(
        self,
        *,
        database: Database,
        password_hasher: PasswordHasher | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        utc_now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self.database = database
        self.password_hasher = password_hasher or PasswordHasher()
        self.session_ttl_seconds = int(session_ttl_seconds)
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.audit_log = audit_log or AuditLog(database, utc_now=self.utc_now)
        self._dummy_hash = self.password_hasher.hash("maze-dummy-password-value")

    def create_user(self, username: str, password: str) -> User:
        normalized = _normalize_username(username)
        _validate_password(password)
        password_hash = self.password_hasher.hash(password)
        user = User(
            user_id=str(self.id_factory()),
            username=normalized,
            status="active",
        )
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, password_hash, status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user.user_id,
                        user.username,
                        password_hash,
                        user.status,
                        _utc_text(self.utc_now()),
                    ),
                )
                self.audit_log.record(
                    "auth.user_created",
                    actor_user_id=user.user_id,
                    details={"username": user.username},
                    connection=connection,
                )
        except sqlite3.IntegrityError as exc:
            raise UsernameExistsError(
                f"username already exists: {normalized}"
            ) from exc
        return user

    def login(self, username: str, password: str) -> SessionCredentials:
        normalized = _normalize_username(username)
        row = self._user_row(normalized)
        candidate_hash = row["password_hash"] if row is not None else self._dummy_hash
        valid = False
        try:
            valid = self.password_hasher.verify(candidate_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            valid = False

        if row is None or not valid or row["status"] != "active":
            self.audit_log.record(
                "auth.login_failed",
                details={"username": normalized},
            )
            raise AuthenticationError("invalid username or password")

        if self.password_hasher.check_needs_rehash(row["password_hash"]):
            with self.database.connection() as connection:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, updated_at_utc = ?
                    WHERE id = ?
                    """,
                    (
                        self.password_hasher.hash(password),
                        _utc_text(self.utc_now()),
                        row["id"],
                    ),
                )

        session_token = str(self.token_factory())
        csrf_token = str(self.token_factory())
        if not session_token or not csrf_token or session_token == csrf_token:
            raise RuntimeError("token factory returned unsafe session tokens")
        created_at = _aware_utc(self.utc_now())
        expires_at = created_at + timedelta(seconds=self.session_ttl_seconds)
        session_id = str(self.id_factory())
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_digest, csrf_digest,
                    expires_at_utc, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    row["id"],
                    _token_digest(session_token),
                    _token_digest(csrf_token),
                    _utc_text(expires_at),
                    _utc_text(created_at),
                ),
            )
            self.audit_log.record(
                "auth.login_succeeded",
                actor_user_id=row["id"],
                session_id=session_id,
                connection=connection,
            )
        return SessionCredentials(
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            user=User(
                user_id=row["id"],
                username=row["username"],
                status=row["status"],
            ),
        )

    def resolve_session(self, session_token: str | None) -> SessionPrincipal:
        if not session_token:
            raise AuthenticationError("authentication required")
        digest = _token_digest(session_token)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    sessions.id AS session_id,
                    sessions.expires_at_utc,
                    sessions.revoked_at_utc,
                    users.id AS user_id,
                    users.username,
                    users.status
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_digest = ?
                """,
                (digest,),
            ).fetchone()
            if row is None or row["revoked_at_utc"] is not None:
                raise AuthenticationError("authentication required")
            expires_at = _parse_utc(row["expires_at_utc"])
            if expires_at <= _aware_utc(self.utc_now()):
                connection.execute(
                    """
                    UPDATE sessions
                    SET revoked_at_utc = COALESCE(revoked_at_utc, ?)
                    WHERE id = ?
                    """,
                    (_utc_text(self.utc_now()), row["session_id"]),
                )
                self.audit_log.record(
                    "auth.session_expired",
                    actor_user_id=row["user_id"],
                    session_id=row["session_id"],
                    connection=connection,
                )
                connection.commit()
                raise AuthenticationError("session expired")
            if row["status"] != "active":
                raise AuthenticationError("account is inactive")
        return SessionPrincipal(
            user_id=row["user_id"],
            username=row["username"],
            session_id=row["session_id"],
            expires_at=expires_at,
        )

    def verify_csrf(
        self,
        principal: SessionPrincipal,
        csrf_token: str | None,
    ) -> bool:
        if not csrf_token:
            return False
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT csrf_digest, revoked_at_utc
                FROM sessions
                WHERE id = ? AND user_id = ?
                """,
                (principal.session_id, principal.user_id),
            ).fetchone()
        return bool(
            row is not None
            and row["revoked_at_utc"] is None
            and hmac.compare_digest(row["csrf_digest"], _token_digest(csrf_token))
        )

    def logout(self, principal: SessionPrincipal) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET revoked_at_utc = COALESCE(revoked_at_utc, ?)
                WHERE id = ? AND user_id = ?
                """,
                (
                    _utc_text(self.utc_now()),
                    principal.session_id,
                    principal.user_id,
                ),
            )
            self.audit_log.record(
                "auth.logout",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                connection=connection,
            )

    def _user_row(self, normalized_username: str):
        with self.database.connection() as connection:
            return connection.execute(
                """
                SELECT id, username, password_hash, status
                FROM users
                WHERE username = ?
                """,
                (normalized_username,),
            ).fetchone()


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: float = 60,
        block_seconds: float = 300,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures <= 0 or window_seconds <= 0 or block_seconds <= 0:
            raise ValueError("rate limiter settings must be positive")
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.block_seconds = float(block_seconds)
        self.monotonic = monotonic
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = RLock()

    def check(self, key: str) -> None:
        now = self.monotonic()
        with self._lock:
            blocked_until = self._blocked_until.get(key, 0.0)
            if blocked_until > now:
                raise RateLimitExceeded(max(1, ceil(blocked_until - now)))
            if blocked_until:
                self._blocked_until.pop(key, None)
                self._failures.pop(key, None)

    def record_failure(self, key: str) -> None:
        now = self.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            failures = [
                timestamp
                for timestamp in self._failures.get(key, [])
                if timestamp >= cutoff
            ]
            failures.append(now)
            self._failures[key] = failures
            if len(failures) >= self.max_failures:
                self._blocked_until[key] = now + self.block_seconds

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._blocked_until.pop(key, None)


def _normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise UsernamePolicyError("username must be a string")
    normalized = username.strip().casefold()
    if USERNAME_PATTERN.fullmatch(normalized) is None:
        raise UsernamePolicyError(
            "username must be 3-64 lowercase letters, numbers, dots, dashes, or underscores"
        )
    return normalized


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise PasswordPolicyError("password must be a string")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password length must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH}"
        )


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
