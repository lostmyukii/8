"""SQLite-backed single-controller lease for two-user operation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Callable

from .audit_log import AuditLog
from .auth import SessionPrincipal
from .database import Database


class LeaseUnavailableError(RuntimeError):
    def __init__(self, status: dict) -> None:
        super().__init__("control lease is already held")
        self.status = status


class LeasePermissionError(RuntimeError):
    """Raised when a caller is not the current lease holder."""


@dataclass(frozen=True)
class LeaseGrant:
    lease_token: str
    expires_at: datetime
    remaining_seconds: int
    renew_after_seconds: int
    lease_seconds: int

    def to_dict(self) -> dict:
        return {
            "lease_token": self.lease_token,
            "expires_at": _utc_text(self.expires_at),
            "remaining_seconds": self.remaining_seconds,
            "renew_after_seconds": self.renew_after_seconds,
            "lease_seconds": self.lease_seconds,
        }


class ControlLeaseService:
    def __init__(
        self,
        *,
        database: Database,
        lease_seconds: int = 15,
        renew_after_seconds: int = 5,
        utc_now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not 0 < renew_after_seconds < lease_seconds:
            raise ValueError("renew_after_seconds must be less than lease_seconds")
        self.database = database
        self.lease_seconds = int(lease_seconds)
        self.renew_after_seconds = int(renew_after_seconds)
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self.audit_log = audit_log or AuditLog(database, utc_now=self.utc_now)

    def claim(self, principal: SessionPrincipal) -> LeaseGrant:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            if row is not None and row["holder_user_id"] is not None:
                expires_at = _parse_optional_utc(row["expires_at_utc"])
                if expires_at is not None and expires_at > now:
                    raise LeaseUnavailableError(self._status_from_row(row, now))
                self._record_expired(connection, row)

            token = str(self.token_factory())
            if not token:
                raise RuntimeError("token factory returned an empty lease token")
            expires_at = now + timedelta(seconds=self.lease_seconds)
            connection.execute(
                """
                INSERT INTO control_lease (
                    singleton_id, holder_user_id, holder_session_id,
                    lease_token_digest, acquired_at_utc,
                    heartbeat_at_utc, expires_at_utc
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    holder_user_id = excluded.holder_user_id,
                    holder_session_id = excluded.holder_session_id,
                    lease_token_digest = excluded.lease_token_digest,
                    acquired_at_utc = excluded.acquired_at_utc,
                    heartbeat_at_utc = excluded.heartbeat_at_utc,
                    expires_at_utc = excluded.expires_at_utc
                """,
                (
                    principal.user_id,
                    principal.session_id,
                    _token_digest(token),
                    _utc_text(now),
                    _utc_text(now),
                    _utc_text(expires_at),
                ),
            )
            self.audit_log.record(
                "control.claimed",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                details={"expires_at": _utc_text(expires_at)},
                connection=connection,
            )
        return LeaseGrant(
            lease_token=token,
            expires_at=expires_at,
            remaining_seconds=self.lease_seconds,
            renew_after_seconds=self.renew_after_seconds,
            lease_seconds=self.lease_seconds,
        )

    def heartbeat(
        self,
        principal: SessionPrincipal,
        lease_token: str,
    ) -> LeaseGrant:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_locked(connection, principal, lease_token, now)
            expires_at = now + timedelta(seconds=self.lease_seconds)
            connection.execute(
                """
                UPDATE control_lease
                SET heartbeat_at_utc = ?, expires_at_utc = ?
                WHERE singleton_id = 1
                """,
                (_utc_text(now), _utc_text(expires_at)),
            )
            self.audit_log.record(
                "control.heartbeat",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                details={"expires_at": _utc_text(expires_at)},
                connection=connection,
            )
        return LeaseGrant(
            lease_token=lease_token,
            expires_at=expires_at,
            remaining_seconds=self.lease_seconds,
            renew_after_seconds=self.renew_after_seconds,
            lease_seconds=self.lease_seconds,
        )

    def release(
        self,
        principal: SessionPrincipal,
        lease_token: str,
    ) -> None:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_locked(connection, principal, lease_token, now)
            self._clear(connection)
            self.audit_log.record(
                "control.released",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                connection=connection,
            )

    def require_holder(
        self,
        principal: SessionPrincipal,
        lease_token: str | None,
    ) -> None:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_locked(connection, principal, lease_token, now)

    def status(self) -> dict:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._active_row(connection, now)
            return self._status_from_row(row, now)

    def status_for(self, principal: SessionPrincipal) -> dict:
        now = _aware_utc(self.utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._active_row(connection, now)
            status = self._status_from_row(row, now)
            status["role"] = (
                "controller"
                if row is not None
                and row["holder_user_id"] == principal.user_id
                and row["holder_session_id"] == principal.session_id
                else "viewer"
            )
            return status

    def audit_operation(
        self,
        principal: SessionPrincipal,
        operation: str,
        *,
        details: dict | None = None,
    ) -> None:
        self.audit_log.record(
            f"control.{operation}",
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
            details=details,
        )

    def _require_locked(
        self,
        connection,
        principal: SessionPrincipal,
        lease_token: str | None,
        now: datetime,
    ):
        row = self._row(connection)
        if row is None or row["holder_user_id"] is None:
            raise LeasePermissionError("control lease required")
        expires_at = _parse_optional_utc(row["expires_at_utc"])
        if expires_at is None or expires_at <= now:
            self._record_expired(connection, row)
            self._clear(connection)
            connection.commit()
            raise LeasePermissionError("control lease expired")
        valid = (
            row["holder_user_id"] == principal.user_id
            and row["holder_session_id"] == principal.session_id
            and bool(lease_token)
            and hmac.compare_digest(
                row["lease_token_digest"],
                _token_digest(str(lease_token)),
            )
        )
        if not valid:
            raise LeasePermissionError("current control lease required")
        return row

    def _record_expired(self, connection, row) -> None:
        if row["holder_user_id"] is None:
            return
        self.audit_log.record(
            "control.expired",
            actor_user_id=row["holder_user_id"],
            session_id=row["holder_session_id"],
            connection=connection,
        )

    def _active_row(self, connection, now: datetime):
        row = self._row(connection)
        if row is None or row["holder_user_id"] is None:
            return None
        expires_at = _parse_optional_utc(row["expires_at_utc"])
        if expires_at is not None and expires_at > now:
            return row
        self._record_expired(connection, row)
        self._clear(connection)
        return None

    @staticmethod
    def _clear(connection) -> None:
        connection.execute(
            """
            UPDATE control_lease
            SET holder_user_id = NULL,
                holder_session_id = NULL,
                lease_token_digest = NULL,
                acquired_at_utc = NULL,
                heartbeat_at_utc = NULL,
                expires_at_utc = NULL
            WHERE singleton_id = 1
            """
        )

    @staticmethod
    def _row(connection):
        return connection.execute(
            """
            SELECT
                control_lease.*,
                users.username AS holder_username
            FROM control_lease
            LEFT JOIN users ON users.id = control_lease.holder_user_id
            WHERE singleton_id = 1
            """
        ).fetchone()

    def _status_from_row(self, row, now: datetime) -> dict:
        if row is None or row["holder_user_id"] is None:
            return {
                "holder": None,
                "expires_at": None,
                "remaining_seconds": 0,
                "renew_after_seconds": self.renew_after_seconds,
                "lease_seconds": self.lease_seconds,
            }
        expires_at = _parse_optional_utc(row["expires_at_utc"])
        remaining = (
            max(0, ceil((expires_at - now).total_seconds()))
            if expires_at is not None
            else 0
        )
        return {
            "holder": {
                "user_id": row["holder_user_id"],
                "username": row["holder_username"],
            },
            "expires_at": _utc_text(expires_at) if expires_at is not None else None,
            "remaining_seconds": remaining,
            "renew_after_seconds": self.renew_after_seconds,
            "lease_seconds": self.lease_seconds,
        }


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_optional_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
