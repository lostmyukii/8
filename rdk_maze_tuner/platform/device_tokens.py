"""High-entropy, digest-only credentials for outbound RDK Agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from .database import Database


DEVICE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")


class DeviceTokenError(RuntimeError):
    """Base device credential error."""


class DeviceAuthenticationError(DeviceTokenError):
    """Raised when a device credential is invalid or revoked."""


class DeviceConflictError(DeviceTokenError):
    """Raised when registering a duplicate device identity."""


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: str
    name: str
    metadata: Mapping[str, object]


class DeviceTokenService:
    """Issue token plaintext once and persist only a SHA-256 digest."""

    def __init__(
        self,
        *,
        database: Database,
        token_factory: Callable[[], str] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.token_factory = token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        device_id: str | None = None,
        name: str,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_id = _device_id(
            device_id or f"rdk-{uuid.uuid4().hex[:12]}"
        )
        normalized_name = _required_text(name, "name", maximum=80)
        token = self._new_token()
        created_at = _utc_text(self.utc_now())
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO devices (
                        id, name, token_digest, status, metadata_json,
                        created_at_utc, updated_at_utc, revoked_at_utc
                    ) VALUES (?, ?, ?, 'offline', ?, ?, ?, NULL)
                    """,
                    (
                        normalized_id,
                        normalized_name,
                        _digest(token),
                        _json(metadata or {}),
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DeviceConflictError(
                f"device already exists: {normalized_id}"
            ) from exc
        return {
            "device_id": normalized_id,
            "name": normalized_name,
            "token": token,
            "created_at_utc": created_at,
        }

    def authenticate(
        self,
        device_id: str,
        token: str,
    ) -> DevicePrincipal:
        normalized_id = _device_id(device_id)
        if not isinstance(token, str) or not token:
            raise DeviceAuthenticationError("invalid device credentials")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, name, token_digest, metadata_json, revoked_at_utc
                FROM devices
                WHERE id = ?
                """,
                (normalized_id,),
            ).fetchone()
        if (
            row is None
            or row["revoked_at_utc"] is not None
            or not isinstance(row["token_digest"], str)
            or not hmac.compare_digest(
                row["token_digest"],
                _digest(token),
            )
        ):
            raise DeviceAuthenticationError("invalid device credentials")
        return DevicePrincipal(
            device_id=row["id"],
            name=row["name"],
            metadata=json.loads(row["metadata_json"]),
        )

    def rotate(self, device_id: str) -> dict[str, object]:
        normalized_id = _device_id(device_id)
        token = self._new_token()
        updated_at = _utc_text(self.utc_now())
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE devices
                SET token_digest = ?, status = 'offline',
                    updated_at_utc = ?, revoked_at_utc = NULL
                WHERE id = ?
                """,
                (_digest(token), updated_at, normalized_id),
            )
        if cursor.rowcount != 1:
            raise DeviceTokenError(
                f"device does not exist: {normalized_id}"
            )
        return {
            "device_id": normalized_id,
            "token": token,
            "rotated_at_utc": updated_at,
        }

    def revoke(self, device_id: str) -> dict[str, object]:
        normalized_id = _device_id(device_id)
        revoked_at = _utc_text(self.utc_now())
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE devices
                SET status = 'revoked', revoked_at_utc = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (revoked_at, revoked_at, normalized_id),
            )
        if cursor.rowcount != 1:
            raise DeviceTokenError(
                f"device does not exist: {normalized_id}"
            )
        return {
            "device_id": normalized_id,
            "revoked_at_utc": revoked_at,
        }

    def list_devices(self) -> list[dict[str, object]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, status, metadata_json, created_at_utc,
                       updated_at_utc, revoked_at_utc
                FROM devices
                ORDER BY created_at_utc, id
                """
            ).fetchall()
        return [
            {
                "device_id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at_utc": row["created_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
                "revoked_at_utc": row["revoked_at_utc"],
            }
            for row in rows
        ]

    def set_status(self, device_id: str, status: str) -> None:
        normalized_id = _device_id(device_id)
        if status not in {"offline", "online", "lost"}:
            raise ValueError("invalid device status")
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE devices
                SET status = ?, updated_at_utc = ?
                WHERE id = ? AND revoked_at_utc IS NULL
                """,
                (status, _utc_text(self.utc_now()), normalized_id),
            )

    def _new_token(self) -> str:
        token = self.token_factory()
        if not isinstance(token, str) or len(token) < 24:
            raise DeviceTokenError(
                "device token factory must provide at least 24 characters"
            )
        return token


def _device_id(value: object) -> str:
    text = str(value or "").strip()
    if not DEVICE_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "device_id must be 3-64 characters using letters, "
            "numbers, dot, underscore or hyphen"
        )
    return text


def _required_text(value: object, name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{name} must be 1-{maximum} characters")
    return text


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
