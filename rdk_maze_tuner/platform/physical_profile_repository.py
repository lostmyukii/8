"""Immutable SQLite assets backed by the strict Webots YAML profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from simulation.webots.maze_car.physical_config import (
    PhysicalProfile,
    PhysicalProfileRepository as YamlPhysicalProfileRepository,
)

from .database import Database


class PhysicalProfileRepositoryError(RuntimeError):
    """Base error for persisted physical-profile assets."""


class PhysicalProfileNotFoundError(PhysicalProfileRepositoryError):
    """Raised when a persisted profile ID is unknown."""


class PhysicalProfileConflictError(PhysicalProfileRepositoryError):
    """Raised when an immutable profile ID is reused with new content."""


@dataclass(frozen=True)
class StoredPhysicalProfile:
    profile_id: str
    digest: str
    random_seed: int
    snapshot: dict[str, Any]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "digest": self.digest,
            "random_seed": self.random_seed,
            "snapshot": json.loads(
                json.dumps(
                    self.snapshot,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            "created_at_utc": self.created_at_utc,
        }


class PhysicalProfileRepository:
    """Persist validated snapshots without reimplementing YAML rules."""

    def __init__(
        self,
        *,
        database: Database,
        yaml_repository: YamlPhysicalProfileRepository | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.yaml_repository = (
            yaml_repository or YamlPhysicalProfileRepository()
        )
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def sync_from_yaml(self) -> dict[str, int]:
        inserted = 0
        unchanged = 0
        for profile in self.yaml_repository.list_profiles():
            if self.import_profile(profile):
                inserted += 1
            else:
                unchanged += 1
        return {"inserted": inserted, "unchanged": unchanged}

    def import_profile(self, profile: PhysicalProfile) -> bool:
        snapshot_json = profile.canonical_json
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT digest, random_seed, snapshot_json
                FROM physical_profiles
                WHERE profile_id = ?
                """,
                (profile.profile_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["digest"] == profile.digest
                    and int(row["random_seed"]) == profile.random_seed
                    and row["snapshot_json"] == snapshot_json
                ):
                    return False
                raise PhysicalProfileConflictError(
                    "physical profile ID already exists with different "
                    f"content: {profile.profile_id}"
                )
            connection.execute(
                """
                INSERT INTO physical_profiles (
                    profile_id, digest, random_seed, snapshot_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.digest,
                    profile.random_seed,
                    snapshot_json,
                    _utc_text(self.utc_now()),
                ),
            )
        return True

    def get(self, profile_id: str) -> StoredPhysicalProfile:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    profile_id, digest, random_seed, snapshot_json,
                    created_at_utc
                FROM physical_profiles
                WHERE profile_id = ?
                """,
                (str(profile_id),),
            ).fetchone()
        if row is None:
            raise PhysicalProfileNotFoundError(
                f"physical profile does not exist: {profile_id}"
            )
        return _from_row(row)

    def list_profiles(self) -> list[StoredPhysicalProfile]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    profile_id, digest, random_seed, snapshot_json,
                    created_at_utc
                FROM physical_profiles
                ORDER BY profile_id
                """
            ).fetchall()
        return [_from_row(row) for row in rows]


def _from_row(row) -> StoredPhysicalProfile:
    snapshot = json.loads(row["snapshot_json"])
    if not isinstance(snapshot, dict):
        raise PhysicalProfileRepositoryError(
            f"stored profile snapshot is invalid: {row['profile_id']}"
        )
    return StoredPhysicalProfile(
        profile_id=str(row["profile_id"]),
        digest=str(row["digest"]),
        random_seed=int(row["random_seed"]),
        snapshot=snapshot,
        created_at_utc=str(row["created_at_utc"]),
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("utc_now must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
