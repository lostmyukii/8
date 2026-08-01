"""Filesystem configuration for persistent platform data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_DATA_DIR = Path("/srv/maze/shared")


@dataclass(frozen=True)
class PlatformConfig:
    """Resolved data paths without creating them as an import side effect."""

    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "maze-platform.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> "PlatformConfig":
        values = os.environ if env is None else env
        configured_dir = values.get("MAZE_DATA_DIR", "").strip()
        if configured_dir:
            data_dir = Path(configured_dir).expanduser()
            if not data_dir.is_absolute():
                data_dir = Path(project_root) / data_dir
        elif values.get("MAZE_ENV", "").strip().lower() in {"server", "production"}:
            data_dir = SERVER_DATA_DIR
        else:
            data_dir = Path(project_root) / ".local" / "maze-data"
        return cls(data_dir=data_dir)

    def ensure_directories(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
