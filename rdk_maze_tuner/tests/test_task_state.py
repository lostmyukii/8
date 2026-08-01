import pytest

from rdk_maze_tuner.platform.task_state import (
    InvalidTaskTransition,
    TaskStateMachine,
    TaskStatus,
)


def test_happy_path_reaches_completed_and_only_safe_states_switch_mode():
    machine = TaskStateMachine()

    assert machine.status is TaskStatus.IDLE
    assert machine.can_switch_mode is True

    for status in (
        TaskStatus.PREFLIGHT,
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.FINALIZING,
        TaskStatus.COMPLETED,
    ):
        machine.transition(status)

    assert machine.status is TaskStatus.COMPLETED
    assert machine.can_switch_mode is True
    assert machine.snapshot()["revision"] == 5


def test_running_can_pause_only_after_pausing_state():
    machine = TaskStateMachine()
    machine.transition(TaskStatus.PREFLIGHT)
    machine.transition(TaskStatus.READY)
    machine.transition(TaskStatus.RUNNING)

    with pytest.raises(InvalidTaskTransition):
        machine.transition(TaskStatus.PAUSED)

    machine.transition(TaskStatus.PAUSING)
    machine.transition(TaskStatus.PAUSED)

    assert machine.status is TaskStatus.PAUSED
    assert machine.can_switch_mode is False


@pytest.mark.parametrize(
    "terminal",
    [TaskStatus.LOST, TaskStatus.ERROR, TaskStatus.ESTOP],
)
def test_danger_states_never_auto_resume(terminal):
    machine = TaskStateMachine()
    machine.transition(TaskStatus.PREFLIGHT)
    machine.transition(TaskStatus.READY)
    machine.transition(TaskStatus.RUNNING)
    machine.transition(terminal, reason="test")

    with pytest.raises(InvalidTaskTransition):
        machine.transition(TaskStatus.RUNNING)

    assert machine.requires_manual_recovery is True
    assert machine.can_switch_mode is False

    machine.transition(TaskStatus.PREFLIGHT, reason="manual recovery")
    assert machine.status is TaskStatus.PREFLIGHT
