"""Immutable SQLite map versions and digest-addressed source artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from rdk_maze_tuner.core.maze_definition import MapDefinition
from rdk_maze_tuner.core.maze_validation import validate_map_definition

from .database import Database


MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class MapRepositoryError(RuntimeError):
    """Base failure for the immutable map store."""


class MapNotFoundError(MapRepositoryError):
    """Raised when a map or map version does not exist."""


class MapConflictError(MapRepositoryError):
    """Raised when a map write conflicts with persisted state."""


@dataclass(frozen=True)
class MapVersion:
    version_id: str
    map_id: str
    version_number: int
    digest: str
    definition: MapDefinition
    created_by_user_id: str | None
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "map_id": self.map_id,
            "version_number": self.version_number,
            "digest": self.digest,
            "definition": self.definition.to_dict(),
            "created_by_user_id": self.created_by_user_id,
            "created_at_utc": self.created_at_utc,
        }


class MapRepository:
    def __init__(
        self,
        *,
        database: Database,
        artifacts_dir: Path,
        id_factory: Callable[[], str] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.artifacts_dir = Path(artifacts_dir)
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def create_map(
        self,
        *,
        name: str,
        definition: Mapping[str, Any],
        created_by_user_id: str | None,
    ) -> tuple[dict[str, Any], MapVersion]:
        normalized_name = _map_name(name)
        parsed = validate_map_definition(definition)
        map_id = f"map-{self.id_factory()}"
        version_id = f"mapv-{self.id_factory()}"
        created_at = _utc_text(self.utc_now())
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO maps (
                    id, name, owner_user_id, created_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    map_id,
                    normalized_name,
                    created_by_user_id,
                    created_at,
                ),
            )
            self._insert_version(
                connection,
                version_id=version_id,
                map_id=map_id,
                version_number=1,
                definition=parsed,
                created_by_user_id=created_by_user_id,
                created_at=created_at,
            )
        return (
            {
                "map_id": map_id,
                "name": normalized_name,
                "owner_user_id": created_by_user_id,
                "created_at_utc": created_at,
            },
            MapVersion(
                version_id=version_id,
                map_id=map_id,
                version_number=1,
                digest=parsed.content_digest,
                definition=parsed,
                created_by_user_id=created_by_user_id,
                created_at_utc=created_at,
            ),
        )

    def save_version(
        self,
        *,
        map_id: str,
        definition: Mapping[str, Any],
        created_by_user_id: str | None,
    ) -> MapVersion:
        parsed = validate_map_definition(definition)
        created_at = _utc_text(self.utc_now())
        version_id = f"mapv-{self.id_factory()}"
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM maps WHERE id = ? AND archived_at_utc IS NULL",
                (map_id,),
            ).fetchone()
            if exists is None:
                raise MapNotFoundError(f"map does not exist: {map_id}")
            next_number = connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM map_versions
                WHERE map_id = ?
                """,
                (map_id,),
            ).fetchone()[0]
            self._insert_version(
                connection,
                version_id=version_id,
                map_id=map_id,
                version_number=int(next_number),
                definition=parsed,
                created_by_user_id=created_by_user_id,
                created_at=created_at,
            )
        return MapVersion(
            version_id=version_id,
            map_id=map_id,
            version_number=int(next_number),
            digest=parsed.content_digest,
            definition=parsed,
            created_by_user_id=created_by_user_id,
            created_at_utc=created_at,
        )

    def get_version(self, version_id: str) -> MapVersion:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    id, map_id, version_number, digest, definition_json,
                    created_by_user_id, created_at_utc
                FROM map_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
        if row is None:
            raise MapNotFoundError(
                f"map version does not exist: {version_id}"
            )
        return _version_from_row(row)

    def list_versions(self, map_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM maps WHERE id = ?",
                (map_id,),
            ).fetchone()
            if exists is None:
                raise MapNotFoundError(f"map does not exist: {map_id}")
            rows = connection.execute(
                """
                SELECT
                    id, map_id, version_number, digest, definition_json,
                    created_by_user_id, created_at_utc
                FROM map_versions
                WHERE map_id = ?
                ORDER BY version_number DESC
                """,
                (map_id,),
            ).fetchall()
        return [_version_from_row(row).to_dict() for row in rows]

    def list_maps(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    maps.id AS map_id,
                    maps.name,
                    maps.owner_user_id,
                    maps.created_at_utc,
                    map_versions.id AS version_id,
                    map_versions.version_number,
                    map_versions.digest,
                    map_versions.definition_json,
                    map_versions.created_by_user_id,
                    map_versions.created_at_utc AS version_created_at_utc
                FROM maps
                LEFT JOIN map_versions
                  ON map_versions.id = (
                    SELECT latest.id
                    FROM map_versions AS latest
                    WHERE latest.map_id = maps.id
                    ORDER BY latest.version_number DESC
                    LIMIT 1
                  )
                WHERE maps.archived_at_utc IS NULL
                ORDER BY maps.created_at_utc DESC, maps.id DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            latest = None
            if row["version_id"] is not None:
                latest = MapVersion(
                    version_id=row["version_id"],
                    map_id=row["map_id"],
                    version_number=row["version_number"],
                    digest=row["digest"],
                    definition=validate_map_definition(
                        json.loads(row["definition_json"])
                    ),
                    created_by_user_id=row["created_by_user_id"],
                    created_at_utc=row["version_created_at_utc"],
                ).to_dict()
            result.append(
                {
                    "map_id": row["map_id"],
                    "name": row["name"],
                    "owner_user_id": row["owner_user_id"],
                    "created_at_utc": row["created_at_utc"],
                    "latest_version": latest,
                }
            )
        return result

    def store_source_image(
        self,
        *,
        map_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        if content_type not in IMAGE_EXTENSIONS:
            raise ValueError("source image must be PNG, JPEG or WebP")
        if not content:
            raise ValueError("source image is empty")
        if len(content) > MAX_SOURCE_IMAGE_BYTES:
            raise ValueError("source image exceeds 10 MiB")
        with self.database.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM maps WHERE id = ?",
                (map_id,),
            ).fetchone()
        if exists is None:
            raise MapNotFoundError(f"map does not exist: {map_id}")

        digest = hashlib.sha256(content).hexdigest()
        relative_path = Path("maps") / map_id / "source" / (
            digest + IMAGE_EXTENSIONS[content_type]
        )
        destination = self.artifacts_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise MapConflictError(
                    "digest-addressed source image content mismatch"
                )
        else:
            temporary = destination.with_name(
                f".{destination.name}.{self.id_factory()}.tmp"
            )
            temporary.write_bytes(content)
            os.replace(temporary, destination)

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, created_at_utc FROM artifacts WHERE relative_path = ?",
                (relative_path.as_posix(),),
            ).fetchone()
            if row is None:
                artifact_id = f"artifact-{self.id_factory()}"
                created_at = _utc_text(self.utc_now())
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, kind, relative_path, sha256, metadata_json,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        "map_source_image",
                        relative_path.as_posix(),
                        digest,
                        json.dumps(
                            {
                                "map_id": map_id,
                                "filename": _safe_filename(filename),
                                "content_type": content_type,
                                "size_bytes": len(content),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        created_at,
                    ),
                )
            else:
                artifact_id = row["id"]
                created_at = row["created_at_utc"]
        return {
            "artifact_id": artifact_id,
            "map_id": map_id,
            "relative_path": relative_path.as_posix(),
            "sha256": digest,
            "content_type": content_type,
            "size_bytes": len(content),
            "created_at_utc": created_at,
        }

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        *,
        version_id: str,
        map_id: str,
        version_number: int,
        definition: MapDefinition,
        created_by_user_id: str | None,
        created_at: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO map_versions (
                    id, map_id, version_number, digest, definition_json,
                    created_by_user_id, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    map_id,
                    version_number,
                    definition.content_digest,
                    json.dumps(
                        definition.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    created_by_user_id,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MapConflictError(
                f"could not save map version {map_id} v{version_number}"
            ) from exc


def _version_from_row(row) -> MapVersion:
    definition = validate_map_definition(json.loads(row["definition_json"]))
    if definition.content_digest != row["digest"]:
        raise MapConflictError(
            f"stored digest mismatch for map version {row['id']}"
        )
    return MapVersion(
        version_id=row["id"],
        map_id=row["map_id"],
        version_number=row["version_number"],
        digest=row["digest"],
        definition=definition,
        created_by_user_id=row["created_by_user_id"],
        created_at_utc=row["created_at_utc"],
    )


def _map_name(value: str) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("map name must contain 1 to 80 characters")
    if any(ord(character) < 32 for character in name):
        raise ValueError("map name contains control characters")
    return name


def _safe_filename(value: str) -> str:
    name = Path(str(value or "source-image")).name
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name)
    return cleaned[:120] or "source-image"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("utc_now must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
