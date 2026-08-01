"""Persistent audit events that are not tied to a maze run."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from .database import Database


class AuditLog:
    def __init__(
        self,
        database: Database,
        *,
        utc_now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))

    def record(
        self,
        event_type: str,
        *,
        actor_user_id: str | None = None,
        session_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        connection=None,
    ) -> str:
        if not event_type:
            raise ValueError("event_type must not be empty")
        event_id = str(self.event_id_factory())
        values = (
            event_id,
            _utc_text(self.utc_now()),
            str(event_type),
            actor_user_id,
            session_id,
            _canonical_json(dict(details or {})),
        )
        statement = """
            INSERT INTO audit_events (
                event_id, utc_timestamp, event_type,
                actor_user_id, session_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        if connection is not None:
            connection.execute(statement, values)
        else:
            with self.database.connection() as owned_connection:
                owned_connection.execute(statement, values)
        return event_id


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
