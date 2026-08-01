"""Safe artifact retention scheduling and cleanup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .event_store import SAFE_RUN_ID


RETENTION_DAYS = {
    "video": 30,
    "events_jsonl": 180,
    "raw_metrics": 180,
    "replay_manifest": 180,
}


class RetentionManager:
    def __init__(
        self,
        *,
        database: Database,
        data_dir: Path,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.data_dir = Path(data_dir)
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def schedule_run(self, run_id: str) -> dict[str, str]:
        if SAFE_RUN_ID.fullmatch(str(run_id)) is None:
            raise ValueError("run_id contains unsafe characters")
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT ended_at_utc FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            ended_at = _parse_utc(run["ended_at_utc"]) or self.utc_now()
            rows = connection.execute(
                """
                SELECT id, kind
                FROM artifacts
                WHERE run_id = ? AND retained_until_utc IS NULL
                """,
                (run_id,),
            ).fetchall()
            scheduled = {}
            for row in rows:
                days = RETENTION_DAYS.get(row["kind"], 180)
                deadline = _utc_text(ended_at + timedelta(days=days))
                connection.execute(
                    """
                    UPDATE artifacts
                    SET retained_until_utc = ?
                    WHERE id = ?
                    """,
                    (deadline, row["id"]),
                )
                scheduled[row["kind"]] = deadline
        return scheduled

    def pin_run(self, run_id: str, *, reason: str) -> int:
        if SAFE_RUN_ID.fullmatch(str(run_id)) is None:
            raise ValueError("run_id contains unsafe characters")
        if not str(reason).strip():
            raise ValueError("pin reason is required")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, metadata_json
                FROM artifacts
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                metadata = _metadata(row["metadata_json"])
                metadata["pin_reason"] = str(reason)
                connection.execute(
                    """
                    UPDATE artifacts
                    SET pinned = 1, metadata_json = ?
                    WHERE id = ?
                    """,
                    (_canonical_json(metadata), row["id"]),
                )
        return len(rows)

    def apply(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        now_text = _utc_text(self.utc_now())
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, relative_path, metadata_json, pinned
                FROM artifacts
                WHERE retained_until_utc IS NOT NULL
                  AND retained_until_utc <= ?
                ORDER BY retained_until_utc, id
                """,
                (now_text,),
            ).fetchall()

        deleted = []
        protected = []
        errors = []
        for row in rows:
            artifact_id = row["id"]
            metadata = _metadata(row["metadata_json"])
            if metadata.get("status") == "deleted":
                continue
            if row["pinned"]:
                protected.append(
                    {"artifact_id": artifact_id, "reason": "pinned"}
                )
                continue
            if metadata.get("references"):
                protected.append(
                    {"artifact_id": artifact_id, "reason": "referenced"}
                )
                continue
            try:
                path = self._safe_path(row["relative_path"])
            except ValueError:
                errors.append(
                    {"artifact_id": artifact_id, "reason": "unsafe_path"}
                )
                continue
            if not dry_run:
                try:
                    if path.exists():
                        if not path.is_file():
                            raise OSError("artifact is not a regular file")
                        path.unlink()
                except OSError:
                    errors.append(
                        {
                            "artifact_id": artifact_id,
                            "reason": "delete_failed",
                        }
                    )
                    continue
                metadata["status"] = "deleted"
                metadata["deleted_at_utc"] = now_text
                with self.database.connection() as connection:
                    connection.execute(
                        """
                        UPDATE artifacts
                        SET metadata_json = ?, sha256 = NULL
                        WHERE id = ?
                        """,
                        (_canonical_json(metadata), artifact_id),
                    )
            deleted.append(artifact_id)
        return {
            "dry_run": dry_run,
            "deleted": deleted,
            "protected": protected,
            "errors": errors,
        }

    def _safe_path(self, relative_path: str) -> Path:
        root = self.data_dir.resolve()
        candidate = (self.data_dir / relative_path).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("artifact path escapes data directory")
        return candidate


def _metadata(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
