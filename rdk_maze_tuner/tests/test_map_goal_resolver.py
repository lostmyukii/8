from __future__ import annotations

from dataclasses import replace

import pytest

from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.platform.map_goal_resolver import (
    MapGoalResolutionError,
    MapGoalResolver,
)
from rdk_maze_tuner.platform.map_repository import MapVersion


def boundary(rows: int, cols: int) -> tuple[WallSegment, ...]:
    return (
        *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
        *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
        *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
        *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
    )


def definition(
    *,
    rows: int = 3,
    cols: int = 3,
    start: tuple[int, int] = (0, 2),
    goals: tuple[tuple[int, int], ...] = ((2, 0),),
    internal_walls: tuple[WallSegment, ...] = (),
) -> MapDefinition:
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=450,
        cell_height_mm=450,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(x=start[0], y=start[1], heading="N"),
        goals=goals,
        walls=boundary(rows, cols) + internal_walls,
    )


def version(value: MapDefinition, *, digest: str | None = None) -> MapVersion:
    return MapVersion(
        version_id="mapv-test",
        map_id="map-test",
        version_number=2,
        digest=digest or value.content_digest,
        definition=value,
        created_by_user_id="user-test",
        created_at_utc="2026-08-02T00:00:00.000000Z",
    )


def resolver_for(value: MapVersion) -> MapGoalResolver:
    return MapGoalResolver(map_provider=lambda _version_id: value)


def test_single_goal_snapshot_contains_immutable_source_evidence():
    map_definition = definition(goals=((2, 0),))
    map_version = version(map_definition)
    before = map_definition.to_dict()
    calls: list[str] = []
    resolver = MapGoalResolver(
        map_provider=lambda version_id: (
            calls.append(version_id) or map_version
        )
    )

    resolved = resolver.resolve("mapv-test")

    assert resolved.cell == (2, 0)
    assert resolved.candidate_cells == ((2, 0),)
    assert resolved.source_map_version == "mapv-test"
    assert resolved.source_map_digest == map_definition.content_digest
    assert resolved.resolution == "single"
    assert resolved.path_length_cells == 4
    assert resolved.to_dict() == {
        "type": "map_goal",
        "cell": [2, 0],
        "candidate_cells": [[2, 0]],
        "source_map_version": "mapv-test",
        "source_map_digest": map_definition.content_digest,
        "resolution": "single",
        "path_length_cells": 4,
    }
    assert calls == ["mapv-test"]
    assert map_definition.to_dict() == before


def test_multiple_goals_choose_shortest_reachable_path():
    map_definition = definition(goals=((2, 0), (0, 0)))

    resolved = resolver_for(version(map_definition)).resolve("mapv-test")

    assert resolved.cell == (0, 0)
    assert resolved.path_length_cells == 2
    assert resolved.resolution == "shortest_path"
    assert resolved.candidate_cells == ((0, 0), (2, 0))


def test_equal_length_goals_use_y_then_x_tie_break():
    map_definition = definition(
        start=(1, 1),
        goals=((2, 0), (0, 0)),
    )

    resolved = resolver_for(version(map_definition)).resolve("mapv-test")

    assert resolved.cell == (0, 0)
    assert resolved.path_length_cells == 2


def test_unreachable_candidate_is_ignored_when_another_goal_is_reachable():
    map_definition = definition(
        rows=2,
        cols=2,
        start=(0, 0),
        goals=((1, 0), (1, 1)),
        internal_walls=(WallSegment(1, 0, 1, 1),),
    )

    resolved = resolver_for(version(map_definition)).resolve("mapv-test")

    assert resolved.cell == (1, 1)
    assert resolved.path_length_cells == 2


def test_missing_goal_is_rejected_with_stable_code():
    map_definition = definition(goals=())

    with pytest.raises(MapGoalResolutionError) as captured:
        resolver_for(version(map_definition)).resolve("mapv-test")

    assert captured.value.code == "MAP_GOAL_MISSING"


def test_all_unreachable_goals_are_rejected_with_stable_code():
    map_definition = definition(
        rows=2,
        cols=2,
        start=(0, 0),
        goals=((1, 1),),
        internal_walls=(
            WallSegment(0, 1, 1, 1),
            WallSegment(1, 0, 1, 1),
        ),
    )

    with pytest.raises(MapGoalResolutionError) as captured:
        resolver_for(version(map_definition)).resolve("mapv-test")

    assert captured.value.code == "MAP_GOAL_UNREACHABLE"


def test_digest_mismatch_is_rejected_before_path_resolution():
    map_definition = definition()
    corrupt_version = replace(version(map_definition), digest="0" * 64)

    with pytest.raises(MapGoalResolutionError) as captured:
        resolver_for(corrupt_version).resolve("mapv-test")

    assert captured.value.code == "MAP_DIGEST_MISMATCH"


def test_version_provider_cannot_substitute_a_different_version_id():
    map_definition = definition()
    wrong_version = replace(
        version(map_definition),
        version_id="mapv-other",
    )

    with pytest.raises(MapGoalResolutionError) as captured:
        resolver_for(wrong_version).resolve("mapv-test")

    assert captured.value.code == "MAP_VERSION_MISMATCH"
