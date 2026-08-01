"""Compile one structural map version for the simulator and Webots scene."""

from __future__ import annotations

from dataclasses import dataclass

from rdk_maze_tuner.core.maze_definition import MapDefinition, WallSegment
from rdk_maze_tuner.core.maze_validation import validate_map_definition


Cell = tuple[int, int]
Edge = frozenset[Cell]


@dataclass(frozen=True)
class CompiledMap:
    rows: int
    cols: int
    cell_width_m: float
    cell_height_m: float
    wall_thickness_m: float
    wall_height_m: float
    start_cell: Cell
    start_heading: str
    goals: tuple[Cell, ...]
    internal_walls: frozenset[Edge]
    wall_nodes: tuple[str, ...]
    digest: str


def compile_map(definition: MapDefinition) -> CompiledMap:
    internal_walls = frozenset(
        edge
        for wall in definition.walls
        if (edge := _internal_edge(wall, definition)) is not None
    )
    return CompiledMap(
        rows=definition.rows,
        cols=definition.cols,
        cell_width_m=definition.cell_width_mm / 1_000,
        cell_height_m=definition.cell_height_mm / 1_000,
        wall_thickness_m=definition.wall_thickness_mm / 1_000,
        wall_height_m=definition.wall_height_mm / 1_000,
        start_cell=(definition.start.x, definition.start.y),
        start_heading=definition.start.heading,
        goals=definition.goals,
        internal_walls=internal_walls,
        wall_nodes=tuple(
            _wall_node(index, wall, definition)
            for index, wall in enumerate(definition.walls, start=1)
        ),
        digest=definition.content_digest,
    )


class WebotsMapLoader:
    """Replace the Supervisor-owned MAZE_WALLS group from a compiled map."""

    def __init__(self, supervisor, *, group_def: str = "MAZE_WALLS") -> None:
        group = supervisor.getFromDef(group_def)
        if group is None:
            raise RuntimeError(f"Webots node DEF {group_def} is missing")
        self.children = group.getField("children")
        if self.children is None:
            raise RuntimeError(f"Webots node DEF {group_def} has no children")

    def load(self, definition: MapDefinition) -> CompiledMap:
        compiled = compile_map(definition)
        while self.children.getCount() > 0:
            self.children.removeMF(self.children.getCount() - 1)
        for node in compiled.wall_nodes:
            self.children.importMFNodeFromString(-1, node)
        return compiled


def default_map_definition() -> MapDefinition:
    """Return the digest-backed open 5x5 map shared by both simulators."""

    return validate_map_definition(
        {
            "rows": 5,
            "cols": 5,
            "cell_width_mm": 450,
            "cell_height_mm": 450,
            "wall_thickness_mm": 40,
            "wall_height_mm": 180,
            "start": {"x": 0, "y": 4, "heading": "N"},
            "goals": [{"x": 4, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 5, "y2": 0},
                {"x1": 5, "y1": 0, "x2": 5, "y2": 5},
                {"x1": 5, "y1": 5, "x2": 0, "y2": 5},
                {"x1": 0, "y1": 5, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )


def calibration_map_definition() -> MapDefinition:
    """Return a centered 3x3 arena with known wall distances."""

    return validate_map_definition(
        {
            "rows": 3,
            "cols": 3,
            "cell_width_mm": 450,
            "cell_height_mm": 450,
            "wall_thickness_mm": 40,
            "wall_height_mm": 180,
            "start": {"x": 1, "y": 1, "heading": "N"},
            "goals": [{"x": 1, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 3, "y2": 0},
                {"x1": 3, "y1": 0, "x2": 3, "y2": 3},
                {"x1": 3, "y1": 3, "x2": 0, "y2": 3},
                {"x1": 0, "y1": 3, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )


def _internal_edge(
    wall: WallSegment,
    definition: MapDefinition,
) -> Edge | None:
    if wall.orientation == "H":
        if wall.y1 in {0, definition.rows}:
            return None
        return frozenset(
            (
                (wall.x1, wall.y1 - 1),
                (wall.x1, wall.y1),
            )
        )
    if wall.x1 in {0, definition.cols}:
        return None
    return frozenset(
        (
            (wall.x1 - 1, wall.y1),
            (wall.x1, wall.y1),
        )
    )


def _wall_node(
    index: int,
    wall: WallSegment,
    definition: MapDefinition,
) -> str:
    cell_width_m = definition.cell_width_mm / 1_000
    cell_height_m = definition.cell_height_mm / 1_000
    thickness_m = definition.wall_thickness_mm / 1_000
    height_m = definition.wall_height_mm / 1_000
    maze_width_m = definition.cols * cell_width_m
    maze_height_m = definition.rows * cell_height_m

    if wall.orientation == "H":
        x = ((wall.x1 + wall.x2) / 2) * cell_width_m - maze_width_m / 2
        z = wall.y1 * cell_height_m - maze_height_m / 2
        size_x = cell_width_m + thickness_m
        size_z = thickness_m
    else:
        x = wall.x1 * cell_width_m - maze_width_m / 2
        z = ((wall.y1 + wall.y2) / 2) * cell_height_m - maze_height_m / 2
        size_x = thickness_m
        size_z = cell_height_m + thickness_m

    return f"""
Solid {{
  translation {_number(x)} {_number(height_m / 2)} {_number(z)}
  children [
    Shape {{
      appearance PBRAppearance {{
        baseColor 0.16 0.55 0.8
        roughness 0.45
        metalness 0.05
      }}
      geometry Box {{
        size {_number(size_x)} {_number(height_m)} {_number(size_z)}
      }}
    }}
  ]
  name "map wall {index}"
  boundingObject Box {{
    size {_number(size_x)} {_number(height_m)} {_number(size_z)}
  }}
}}
""".strip()


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
