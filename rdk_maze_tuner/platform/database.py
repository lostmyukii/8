"""SQLite connection lifecycle and explicit schema migrations."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


DEFAULT_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]+)_(?P<name>.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


class Database:
    def __init__(
        self,
        path: Path,
        *,
        migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.busy_timeout_ms = int(busy_timeout_ms)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> tuple[int, ...]:
        """Apply each numbered migration once and return versions applied now."""

        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at_utc TEXT NOT NULL
                )
                """
            )

        applied: list[int] = []
        for migration in self._discover_migrations():
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (migration.version,),
                ).fetchone()
                if exists:
                    continue
                for statement in _sql_statements(
                    migration.path.read_text(encoding="utf-8")
                ):
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at_utc)
                    VALUES (?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        _utc_now_text(),
                    ),
                )
                applied.append(migration.version)
        return tuple(applied)

    def _discover_migrations(self) -> tuple[Migration, ...]:
        if not self.migrations_dir.is_dir():
            raise FileNotFoundError(
                f"migration directory does not exist: {self.migrations_dir}"
            )
        migrations: list[Migration] = []
        for path in self.migrations_dir.glob("*.sql"):
            match = MIGRATION_PATTERN.fullmatch(path.name)
            if not match:
                raise ValueError(f"invalid migration filename: {path.name}")
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=path.name,
                    path=path,
                )
            )
        migrations.sort(key=lambda item: item.version)
        versions = [migration.version for migration in migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("duplicate migration version")
        return tuple(migrations)


def _sql_statements(script: str) -> Iterator[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise ValueError("incomplete SQL migration statement")


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
