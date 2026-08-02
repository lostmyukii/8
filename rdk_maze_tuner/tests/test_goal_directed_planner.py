from __future__ import annotations

import pytest

from rdk_maze_tuner.core.goal_directed_planner import (
    NO_PATH,
    GoalDirectedPlanner,
    GoalPlanningError,
)
from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap


def boundary(rows: int, cols: int) -> tuple[WallSegment, ...]:
    return (
        *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
        *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
        *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
        *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
    )


def maze_for(
    *,
    rows: int = 5,
    cols: int = 5,
    start: tuple[int, int] = (0, 4),
    heading: Direction = Direction.NORTH,
    goals: tuple[tuple[int, int], ...] = ((4, 0),),
    internal_walls: tuple[WallSegment, ...] = (
        WallSegment(0, 4, 1, 4),
    ),
) -> MazeMap:
    definition = MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=450,
        cell_height_mm=450,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(x=start[0], y=start[1], heading=heading.value),
        goals=goals,
        walls=boundary(rows, cols) + internal_walls,
    )
    return MazeMap.from_definition(
        definition,
        wall_threshold_mm=150,
        map_version_id="mapv-route",
    )


def test_plans_deterministic_legal_route_from_task12_start_to_map_goal():
    maze = maze_for()
    planner = GoalDirectedPlanner(goal_cells=((4, 0),))

    route = planner.plan_route(maze)

    assert route.start == (0, 4)
    assert route.start_heading == Direction.NORTH
    assert route.goal == (4, 0)
    assert route.cells == (
        (0, 4),
        (1, 4),
        (1, 3),
        (1, 2),
        (1, 1),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
    )
    assert [action.name for action in route.actions] == [
        "turn_right",
        "move_cell",
        "turn_left",
        "move_cell",
        "move_cell",
        "move_cell",
        "move_cell",
        "turn_right",
        "move_cell",
        "move_cell",
        "move_cell",
    ]

    cursor = maze.position
    heading = maze.heading
    moved_cells = [cursor]
    for action in route.actions:
        maze.position = cursor
        maze.heading = heading
        if action.name == "move_cell":
            direction = action.direction or heading
            assert maze.wall_for_planning(cursor, direction) is False
            target = maze.neighbor(cursor, direction)
            assert 0 <= target[0] < maze.cols
            assert 0 <= target[1] < maze.rows
            cursor = target
            moved_cells.append(cursor)
        else:
            maze.apply_completed_action(action)
            heading = maze.heading
    assert tuple(moved_cells) == route.cells

    same_route = GoalDirectedPlanner(
        goal_cells=((4, 0),),
    ).plan_route(maze_for())
    assert same_route.to_dict() == route.to_dict()


def test_equal_distance_goal_tie_break_is_y_then_x():
    maze = maze_for(
        start=(2, 2),
        goals=((4, 2), (2, 0), (0, 2)),
        internal_walls=(),
    )
    planner = GoalDirectedPlanner(
        goal_cells=((4, 2), (2, 0), (0, 2)),
    )

    route = planner.plan_route(maze)

    assert route.goal == (2, 0)
    assert route.cells == ((2, 2), (2, 1), (2, 0))


def test_goal_position_stops_without_motion():
    maze = maze_for(start=(4, 0), internal_walls=())
    planner = GoalDirectedPlanner(goal_cells=((4, 0),))

    action = planner.next_action(maze)

    assert action.name == "stop"
    assert planner.last_route is not None
    assert planner.last_route.cells == ((4, 0),)
    assert planner.last_route.actions == ()


def test_current_reliable_pose_change_forces_replan_instead_of_stale_queue():
    maze = maze_for()
    planner = GoalDirectedPlanner(goal_cells=((4, 0),))

    first = planner.next_action(maze)
    assert first.name == "turn_right"
    maze.position = (0, 3)
    maze.heading = Direction.NORTH
    second = planner.next_action(maze)

    assert second.name == "move_cell"
    assert second.direction == Direction.NORTH
    assert planner.last_route is not None
    assert planner.last_route.start == (0, 3)
    assert planner.last_route.cells[1] == (0, 2)


def test_no_path_has_stable_error_code_and_exhausted_action():
    maze = maze_for(
        rows=2,
        cols=2,
        start=(0, 1),
        goals=((1, 0),),
        internal_walls=(
            WallSegment(0, 1, 1, 1),
            WallSegment(1, 1, 1, 2),
        ),
    )
    planner = GoalDirectedPlanner(goal_cells=((1, 0),))

    with pytest.raises(GoalPlanningError) as captured:
        planner.plan_route(maze)

    assert captured.value.code == NO_PATH
    assert planner.next_action(maze).name == "stop"
    assert planner.last_error is not None
    assert planner.last_error.code == NO_PATH
