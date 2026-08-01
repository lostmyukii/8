"""Immutable, canonical definition of a rectangular grid maze."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, order=True)
class WallSegment:
    """One unit horizontal or vertical wall on the snapped grid."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def orientation(self) -> str:
        return "H" if self.y1 == self.y2 else "V"

    def to_dict(self) -> dict[str, int]:
        return {
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
        }


@dataclass(frozen=True)
class StartPose:
    x: int
    y: int
    heading: str

    def to_dict(self) -> dict[str, int | str]:
        return {"x": self.x, "y": self.y, "heading": self.heading}


@dataclass(frozen=True)
class MapDefinition:
    """Validated map content; identity is the SHA-256 of canonical content."""

    rows: int
    cols: int
    cell_width_mm: int
    cell_height_mm: int
    wall_thickness_mm: int
    wall_height_mm: int
    start: StartPose
    goals: tuple[tuple[int, int], ...]
    walls: tuple[WallSegment, ...]
    source_image_digest: str | None = None

    @property
    def content_digest(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "cell_width_mm": self.cell_width_mm,
            "cell_height_mm": self.cell_height_mm,
            "wall_thickness_mm": self.wall_thickness_mm,
            "wall_height_mm": self.wall_height_mm,
            "start": self.start.to_dict(),
            "goals": [
                {"x": x, "y": y}
                for x, y in self.goals
            ],
            "walls": [wall.to_dict() for wall in self.walls],
            "source_image_digest": self.source_image_digest,
        }

    def wall_keys(self) -> frozenset[tuple[str, int, int]]:
        return frozenset(_wall_key(wall) for wall in self.walls)

    def blocked_directions(
        self,
        cell: tuple[int, int],
    ) -> frozenset[str]:
        """Return N/E/S/W walls using editor coordinates where y grows down."""

        x, y = cell
        keys = self.wall_keys()
        blocked: set[str] = set()
        if ("H", y, x) in keys:
            blocked.add("N")
        if ("V", x + 1, y) in keys:
            blocked.add("E")
        if ("H", y + 1, x) in keys:
            blocked.add("S")
        if ("V", x, y) in keys:
            blocked.add("W")
        return frozenset(blocked)

    def iter_cells(self) -> Iterable[tuple[int, int]]:
        for y in range(self.rows):
            for x in range(self.cols):
                yield x, y


def _wall_key(wall: WallSegment) -> tuple[str, int, int]:
    if wall.orientation == "H":
        return "H", wall.y1, wall.x1
    return "V", wall.x1, wall.y1
