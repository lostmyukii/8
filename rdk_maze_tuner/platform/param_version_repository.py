"""Immutable parameter snapshots with an explicit automatic-change policy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from .database import Database


AUTOMATIC_SOURCES = {
    "agent_auto",
    "auto_tune",
    "model",
    "reinforcement",
}
FORBIDDEN_AUTOMATIC_EXACT = {
    "motor.max_pwm",
    "tof.front_stop_mm",
    "tof.danger_stop_mm",
}
FORBIDDEN_AUTOMATIC_PREFIXES = (
    "safety.",
    "arrival_verification.",
)


class ParamVersionError(RuntimeError):
    """Base immutable parameter store error."""


class ParamVersionNotFoundError(ParamVersionError):
    """Raised when a parameter version cannot be resolved."""


class ParamPolicyError(ParamVersionError):
    """Raised when an automatic version crosses a safety boundary."""


@dataclass(frozen=True)
class ParamVersion:
    version_id: str
    parent_id: str | None
    digest: str
    snapshot: dict[str, Any]
    diff: dict[str, list[Any]]
    source: str
    evidence: dict[str, Any]
    approval: dict[str, Any]
    context: dict[str, Any]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "parent_id": self.parent_id,
            "digest": self.digest,
            "snapshot": _clone(self.snapshot),
            "diff": _clone(self.diff),
            "source": self.source,
            "evidence": _clone(self.evidence),
            "approval": _clone(self.approval),
            "context": _clone(self.context),
            "created_at_utc": self.created_at_utc,
        }


class ParamVersionRepository:
    def __init__(
        self,
        *,
        database: Database,
        id_factory: Callable[[], str] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.id_factory = id_factory or (
            lambda: f"param-{uuid.uuid4()}"
        )
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def create(
        self,
        *,
        snapshot: Mapping[str, Any],
        source: str,
        parent_id: str | None = None,
        version_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        approval: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ParamVersion:
        normalized_snapshot = _mapping(snapshot, "snapshot")
        normalized_source = str(source or "").strip()
        if not normalized_source:
            raise ValueError("source is required")
        parent = None if parent_id is None else self.get(parent_id)
        diff = _diff(
            {} if parent is None else parent.snapshot,
            normalized_snapshot,
        )
        if normalized_source in AUTOMATIC_SOURCES:
            forbidden = next(
                (
                    path
                    for path in sorted(diff)
                    if path in FORBIDDEN_AUTOMATIC_EXACT
                    or path.startswith(FORBIDDEN_AUTOMATIC_PREFIXES)
                ),
                None,
            )
            if forbidden is not None:
                raise ParamPolicyError(
                    f"automatic source cannot modify {forbidden}"
                )
        digest = _digest(normalized_snapshot)
        identifier = str(version_id or self.id_factory()).strip()
        if not identifier:
            raise ValueError("version_id is required")
        created_at = _utc_text(self.utc_now())
        values = {
            "snapshot": normalized_snapshot,
            "diff": diff,
            "evidence": _mapping(evidence or {}, "evidence"),
            "approval": _mapping(approval or {}, "approval"),
            "context": _mapping(context or {}, "context"),
        }
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO param_versions (
                        id, parent_id, digest, snapshot_json, diff_json,
                        source, evidence_json, approval_json,
                        context_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        parent_id,
                        digest,
                        _json(values["snapshot"]),
                        _json(values["diff"]),
                        normalized_source,
                        _json(values["evidence"]),
                        _json(values["approval"]),
                        _json(values["context"]),
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ParamVersionError(
                f"parameter version conflicts with immutable state: "
                f"{identifier}"
            ) from exc
        return ParamVersion(
            version_id=identifier,
            parent_id=parent_id,
            digest=digest,
            source=normalized_source,
            created_at_utc=created_at,
            **values,
        )

    def ensure(
        self,
        *,
        version_id: str,
        snapshot: Mapping[str, Any],
        source: str = "system_bootstrap",
    ) -> ParamVersion:
        try:
            existing = self.get(version_id)
        except ParamVersionNotFoundError:
            return self.create(
                version_id=version_id,
                snapshot=snapshot,
                source=source,
            )
        expected = _digest(_mapping(snapshot, "snapshot"))
        if existing.digest != expected:
            raise ParamVersionError(
                f"existing {version_id} does not match bootstrap snapshot"
            )
        return existing

    def get(self, version_id: str) -> ParamVersion:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, parent_id, digest, snapshot_json, diff_json,
                       source, evidence_json, approval_json, context_json,
                       created_at_utc
                FROM param_versions
                WHERE id = ?
                """,
                (str(version_id),),
            ).fetchone()
        if row is None:
            raise ParamVersionNotFoundError(
                f"parameter version does not exist: {version_id}"
            )
        return ParamVersion(
            version_id=row["id"],
            parent_id=row["parent_id"],
            digest=row["digest"],
            snapshot=json.loads(row["snapshot_json"]),
            diff=json.loads(row["diff_json"]),
            source=row["source"],
            evidence=json.loads(row["evidence_json"]),
            approval=json.loads(row["approval_json"]),
            context=json.loads(row["context_json"]),
            created_at_utc=row["created_at_utc"],
        )


def _mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return json.loads(_json(value))


def _flatten(
    value: Mapping[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(value):
        path = f"{prefix}.{key}" if prefix else str(key)
        item = value[key]
        if isinstance(item, Mapping):
            result.update(_flatten(item, prefix=path))
        else:
            result[path] = item
    return result


def _diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, list[Any]]:
    old = _flatten(before)
    new = _flatten(after)
    missing = object()
    result: dict[str, list[Any]] = {}
    for path in sorted(set(old) | set(new)):
        first = old.get(path, missing)
        second = new.get(path, missing)
        if first == second:
            continue
        result[path] = [
            None if first is missing else first,
            None if second is missing else second,
        ]
    return result


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _clone(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(_json(value))


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
