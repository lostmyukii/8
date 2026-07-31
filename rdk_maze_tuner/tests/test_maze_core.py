from rdk_maze_tuner.core.maze_map import Direction, MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner


def test_maze_map_converts_local_walls_to_global_walls():
    maze = MazeMap(wall_threshold_mm=150)

    maze.observe(front_mm=100, left_mm=300, right_mm=90)

    cell = maze.cell((0, 0))
    assert cell.walls["N"] is True
    assert cell.walls["W"] is False
    assert cell.walls["E"] is True


def test_planner_waits_for_done_before_advancing_position():
    maze = MazeMap(wall_threshold_mm=150)
    planner = MazePlanner()
    maze.observe(front_mm=300, left_mm=90, right_mm=90)

    action = planner.next_action(maze)

    assert action.name == "move_cell"
    assert maze.position == (0, 0)
    maze.apply_completed_action(action)
    assert maze.position == (0, 1)
    assert maze.heading == Direction.NORTH


def test_planner_turns_before_moving_to_unvisited_side_path():
    maze = MazeMap(wall_threshold_mm=150)
    planner = MazePlanner(priority=("E", "N", "W", "S"))
    maze.observe(front_mm=300, left_mm=90, right_mm=300)

    first = planner.next_action(maze)
    maze.apply_completed_action(first)
    second = planner.next_action(maze)
    maze.apply_completed_action(second)

    assert first.name == "turn_right"
    assert second.name == "move_cell"
    assert maze.position == (1, 0)
    assert maze.heading == Direction.EAST

