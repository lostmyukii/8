from dataclasses import replace

import pytest

from rdk_maze_tuner.core.goal_verifier import (
    GoalVerificationInput,
    GoalVerifier,
)
from rdk_maze_tuner.core.motion_evidence import (
    ArrivalVerificationConfig,
)


def verifier():
    return GoalVerifier(
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v2",
            "source_map_digest": "digest-v2",
        },
        map_version_id="map-v2",
        map_digest="digest-v2",
        cell_width_mm=250.0,
        cell_height_mm=250.0,
        config=ArrivalVerificationConfig(),
    )


def valid_input():
    return GoalVerificationInput(
        logical_cell=(4, 0),
        last_action_id="run-1-0008",
        last_result={
            "type": "done",
            "action_id": "run-1-0008",
            "name": "move_cell",
            "success": True,
        },
        reliable_pose={
            "grid_cell": [4, 0],
            "x_mm": 1125.0,
            "y_mm": 125.0,
            "yaw_deg": 90.0,
            "confidence": 0.92,
        },
        unresolved_faults=(),
    )


def test_goal_verifier_accepts_only_complete_physical_arrival_evidence():
    decision = verifier().verify(valid_input())

    assert decision.verified is True
    assert decision.code is None
    assert decision.position_error_mm == 0.0
    assert all(decision.checks.values())
    assert decision.to_dict()["goal"]["cell"] == [4, 0]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda item: replace(
                item,
                last_result={
                    **item.last_result,
                    "action_id": "other-action",
                },
            ),
            "ACTION_DONE_MISSING",
        ),
        (
            lambda item: replace(item, logical_cell=(3, 0)),
            "LOGICAL_GOAL_MISMATCH",
        ),
        (
            lambda item: replace(
                item,
                reliable_pose={
                    **item.reliable_pose,
                    "grid_cell": [3, 0],
                },
            ),
            "RELIABLE_GOAL_MISMATCH",
        ),
        (
            lambda item: replace(
                item,
                reliable_pose={
                    **item.reliable_pose,
                    "x_mm": 1160.0,
                },
            ),
            "GOAL_POSITION_OUT_OF_TOLERANCE",
        ),
        (
            lambda item: replace(
                item,
                reliable_pose={
                    **item.reliable_pose,
                    "confidence": 0.79,
                },
            ),
            "POSE_UNCERTAIN",
        ),
        (
            lambda item: replace(
                item,
                unresolved_faults=("ESTOP",),
            ),
            "GOAL_BLOCKED_BY_FAULT",
        ),
    ],
)
def test_goal_verifier_rejects_each_missing_evidence(mutation, code):
    decision = verifier().verify(mutation(valid_input()))

    assert decision.verified is False
    assert decision.code == code


def test_goal_verifier_rejects_goal_source_map_identity_mismatch():
    wrong_version = GoalVerifier(
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v1",
            "source_map_digest": "digest-v2",
        },
        map_version_id="map-v2",
        map_digest="digest-v2",
        cell_width_mm=250,
        cell_height_mm=250,
        config=ArrivalVerificationConfig(),
    )
    wrong_digest = GoalVerifier(
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v2",
            "source_map_digest": "digest-v1",
        },
        map_version_id="map-v2",
        map_digest="digest-v2",
        cell_width_mm=250,
        cell_height_mm=250,
        config=ArrivalVerificationConfig(),
    )

    assert wrong_version.verify(valid_input()).code == (
        "MAP_VERSION_MISMATCH"
    )
    assert wrong_digest.verify(valid_input()).code == (
        "MAP_DIGEST_MISMATCH"
    )
