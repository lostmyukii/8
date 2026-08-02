from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rdk_maze_tuner.core.goal_directed_planner import GoalDirectedPlanner
from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.dashboard.app import _planner_for_task

from rdk_maze_tuner.tests.test_goal_directed_planner import maze_for


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")


class OneStepClient:
    def wait_telemetry(self):
        return {
            "type": "telemetry",
            "state": "IDLE",
            "front_mm": 500,
            "left_mm": 90,
            "right_mm": 500,
        }

    def execute_action(self, *, action_id, name, speed, target_ticks):
        return {
            "type": "done",
            "action_id": action_id,
            "name": name,
            "success": True,
            "duration_ms": 100,
            "enc_left": target_ticks,
            "enc_right": target_ticks,
        }


def test_runner_factory_selects_goal_planner_only_for_auto_map_goal():
    automatic = _planner_for_task(
        SimpleNamespace(
            run_kind="auto_to_map_goal",
            goal={"type": "map_goal", "cell": [4, 0]},
        )
    )
    exploration = _planner_for_task(
        SimpleNamespace(
            run_kind="exploration_complete",
            goal={"type": "exploration_complete"},
        )
    )

    assert isinstance(automatic, GoalDirectedPlanner)
    assert automatic.goal_cells == ((4, 0),)
    assert isinstance(exploration, MazePlanner)


def test_maze_runner_emits_route_planned_before_action_event():
    maze = maze_for()
    planner = GoalDirectedPlanner(goal_cells=((4, 0),))
    runner = MazeRunner(
        client=OneStepClient(),
        params=ParamManager(params_path=PARAMS, limits_path=LIMITS),
        maze=maze,
        planner=planner,
    )
    events: list[dict] = []

    result = runner.run_step(
        goal=lambda current, _telemetry: current.position == (4, 0),
        event_sink=events.append,
    )

    event_types = [event["type"] for event in events]
    assert result.action.name == "turn_right"
    assert event_types.index("route.planned") < event_types.index(
        "planned_action"
    )
    route = next(
        event["payload"]
        for event in events
        if event["type"] == "route.planned"
    )
    assert route["map_version_id"] == "mapv-route"
    assert route["start"] == [0, 4]
    assert route["goal"] == [4, 0]
    assert route["cells"][-1] == [4, 0]
    assert route["actions"][0]["name"] == "turn_right"


def test_exploration_planner_keeps_observation_only_dfs_contract():
    maze = MazeMap(wall_threshold_mm=150)
    maze.observe(front_mm=500, left_mm=90, right_mm=90)
    planner = _planner_for_task(
        SimpleNamespace(
            run_kind="exploration_complete",
            goal={"type": "exploration_complete"},
        )
    )

    action = planner.next_action(maze)

    assert action.name == "move_cell"
