import json
from pathlib import Path

import pytest

from simulation.webots.maze_car.tools.goal_acceptance_schema import (
    GoalAcceptanceReportError,
    validate_goal_acceptance_report,
)
from simulation.webots.maze_car.tools.run_goal_acceptance import (
    GoalAcceptanceRunConfig,
    GoalAcceptanceRunner,
    evaluate_truth_safety,
    exit_code_for_report,
)


MAP_DIGEST = (
    "c48518f9f29bda59fd345c87668ff09d"
    "0efeb8bb41e3efd9f401ea8fb9ec485d"
)
PROFILE_DIGEST = "a" * 64


def action(
    action_id: str,
    name: str,
    *,
    recovery: bool = False,
) -> dict:
    return {
        "action_id": action_id,
        "name": name,
        "recovery": recovery,
        "terminal": {
            "type": "done",
            "action_id": action_id,
            "success": True,
        },
    }


def trial(index: int) -> dict:
    actions = [
        action(f"run-{index}-0001", "move_cell"),
        action(f"run-{index}-0002", "turn_right"),
        action(f"run-{index}-0003", "move_cell"),
        action(
            f"run-{index}-0003-recovery-1",
            "move_distance",
            recovery=True,
        ),
        action(f"run-{index}-0004", "move_cell"),
    ]
    return {
        "trial_index": index,
        "status": "PASS",
        "task_id": f"task-{index}",
        "run_id": f"run-{index}",
        "task_status": "COMPLETED",
        "completion_reason": "goal_reached",
        "route": [
            [0, 4],
            [0, 3],
            [1, 3],
            [2, 3],
            [3, 3],
            [4, 3],
            [4, 2],
            [4, 1],
            [4, 0],
        ],
        "action_count": len(actions),
        "actions": actions,
        "turn_count": 1,
        "corrections": [
            {
                "action_id": f"run-{index}-0003-recovery-1",
                "kind": "nudge_forward",
                "before_error": {
                    "position_error_ratio": 0.14,
                    "heading_error_deg": 2.0,
                },
                "after_error": {
                    "position_error_ratio": 0.04,
                    "heading_error_deg": 1.0,
                },
            }
        ],
        "final_pose": {
            "reliable_cell": [4, 0],
            "x_mm": 1800.0,
            "y_mm": 0.0,
            "heading": "N",
            "yaw_deg": 0.5,
            "confidence": 0.94,
        },
        "safety": {
            "truth_sample_count": 200,
            "collision_count": 0,
            "out_of_bounds_count": 0,
            "wall_crossing_count": 0,
            "conflict_count": 0,
        },
        "evidence_sources": [
            "encoder",
            "tof_front",
            "tof_left",
            "tof_right",
            "wall_constraint",
        ],
        "score": {"total_score": 98.0},
        "replay": {
            "schema_version": 2,
            "relative_path": f"trials/{index}/replay.json",
        },
        "raw_events_jsonl": f"trials/{index}/events.jsonl",
    }


def complete_report() -> dict:
    return {
        "schema_version": 1,
        "run_id": "goal-acceptance-test",
        "status": "PASS",
        "source_commit": "b" * 40,
        "webots_version": "R2025a",
        "started_at_utc": "2026-08-02T00:00:00Z",
        "ended_at_utc": "2026-08-02T00:02:00Z",
        "output_dir": "goal-acceptance-test",
        "map": {
            "map_version_id": "task12-public-v2",
            "digest": MAP_DIGEST,
        },
        "param_version": {
            "version_id": "1",
            "digest": "c" * 64,
        },
        "completion_thresholds": {
            "goal_min_confidence": 0.8,
            "nominal_position_error_ratio": 0.1,
            "recoverable_position_error_ratio": 0.2,
            "nominal_heading_error_deg": 8.0,
            "recoverable_heading_error_deg": 12.0,
            "max_recovery_attempts_per_cell": 2,
        },
        "physical_profile": {
            "profile_id": "normal-v1",
            "digest": PROFILE_DIGEST,
            "seed": 20260801,
        },
        "start": {"cell": [0, 4], "heading": "N"},
        "goal": {"cell": [4, 0], "source": "map_primary_goal"},
        "truth_policy": {
            "sim_truth": "evaluation_only",
            "algorithm_evidence_excludes_sim_truth": True,
        },
        "trials": [trial(1), trial(2)],
        "errors": [],
        "artifacts": {
            "report_json": "report.json",
            "events_jsonl": "events.jsonl",
        },
    }


def test_complete_report_requires_two_full_map_goal_trials():
    report = complete_report()

    assert validate_goal_acceptance_report(report) == report

    for field in (
        "source_commit",
        "webots_version",
        "map",
        "param_version",
        "completion_thresholds",
        "physical_profile",
        "start",
        "goal",
        "truth_policy",
        "trials",
        "artifacts",
    ):
        incomplete = dict(report)
        incomplete.pop(field)
        with pytest.raises(GoalAcceptanceReportError):
            validate_goal_acceptance_report(incomplete)


def test_client_goal_override_or_wrong_final_cell_cannot_pass():
    overridden = complete_report()
    overridden["goal"] = {
        "cell": [0, 3],
        "source": "client_override",
    }
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(overridden)

    short = complete_report()
    short["trials"][0]["final_pose"]["reliable_cell"] = [0, 3]
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(short)


def test_done_without_external_evidence_cannot_advance_logical_cell():
    report = complete_report()
    report["trials"][0]["evidence_sources"] = ["encoder"]

    with pytest.raises(
        GoalAcceptanceReportError,
        match="external pose evidence",
    ):
        validate_goal_acceptance_report(report)


def test_conflict_or_sim_truth_dependency_cannot_pass():
    conflicted = complete_report()
    conflicted["trials"][0]["safety"]["conflict_count"] = 1
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(conflicted)

    truth_dependent = complete_report()
    truth_dependent["trials"][0]["evidence_sources"].append("sim_truth")
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(truth_dependent)


@pytest.mark.parametrize(
    "missing",
    ["actions", "corrections", "route", "raw_events_jsonl"],
)
def test_missing_action_correction_route_or_raw_evidence_is_rejected(
    missing,
):
    report = complete_report()
    report["trials"][0].pop(missing)

    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(report)


def test_action_terminal_ids_and_turn_correction_evidence_are_strict():
    mismatched = complete_report()
    mismatched["trials"][0]["actions"][0]["terminal"][
        "action_id"
    ] = "other"
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(mismatched)

    no_turn = complete_report()
    no_turn["trials"][0]["turn_count"] = 0
    no_turn["trials"][0]["actions"][1]["name"] = "move_cell"
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(no_turn)

    worse = complete_report()
    worse["trials"][0]["corrections"][0]["after_error"][
        "position_error_ratio"
    ] = 0.20
    with pytest.raises(GoalAcceptanceReportError):
        validate_goal_acceptance_report(worse)


def test_runner_repeats_fixed_inputs_and_atomically_archives(tmp_path):
    webots = tmp_path / "webots"
    webots.write_text("#!/bin/sh\n", encoding="utf-8")
    webots.chmod(0o755)
    map_path = tmp_path / "map.json"
    map_path.write_text(
        (
            Path(
                "simulation/webots/maze_car/config/maps/"
                "task12-public-v2.json"
            )
            .read_text(encoding="utf-8")
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "goal.yaml"
    config_path.write_text(
        "\n".join(
            [
                "schema_version: 1",
                "map_version_id: task12-public-v2",
                f"map_digest: {MAP_DIGEST}",
                "param_version_id: '1'",
                "physical_profile_id: normal-v1",
                "trials: 2",
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return trial(len(calls))

    runner = GoalAcceptanceRunner(
        GoalAcceptanceRunConfig(
            webots=webots,
            world=Path("world.wbt"),
            map_asset=map_path,
            acceptance_config=config_path,
            output=tmp_path / "acceptance",
            total_timeout_s=30.0,
        ),
        trial_executor=execute,
        source_commit=lambda: "b" * 40,
        webots_version=lambda _path: "R2025a",
        profile_loader=lambda _profile_id: {
            "profile_id": "normal-v1",
            "digest": PROFILE_DIGEST,
            "seed": 20260801,
        },
        param_snapshot=lambda: {
            "version_id": "1",
            "digest": "c" * 64,
            "completion_thresholds": complete_report()[
                "completion_thresholds"
            ],
        },
    )

    report = runner.run()

    assert report["status"] == "PASS"
    assert exit_code_for_report(report) == 0
    assert len(calls) == 2
    assert all(call["map_digest"] == MAP_DIGEST for call in calls)
    assert all(call["seed"] == 20260801 for call in calls)
    final_dir = runner.config.output / report["run_id"]
    assert final_dir.is_dir()
    assert not any(
        path.name.startswith(".tmp-")
        for path in runner.config.output.iterdir()
    )
    assert json.loads(
        (final_dir / "report.json").read_text(encoding="utf-8")
    )["status"] == "PASS"


def test_runner_does_not_fake_pass_when_webots_is_missing(tmp_path):
    runner = GoalAcceptanceRunner(
        GoalAcceptanceRunConfig(
            webots=tmp_path / "missing-webots",
            world=Path("world.wbt"),
            map_asset=Path(
                "simulation/webots/maze_car/config/maps/"
                "task12-public-v2.json"
            ),
            acceptance_config=Path(
                "simulation/webots/maze_car/config/"
                "goal_acceptance.yaml"
            ),
            output=tmp_path / "acceptance",
            total_timeout_s=3.0,
        )
    )

    report = runner.run()

    assert report["status"] == "unavailable"
    assert exit_code_for_report(report) != 0
    assert report["errors"][0]["code"] == "WEBOTS_UNAVAILABLE"


def test_release_gate_runs_p5_before_atomic_current_switch():
    script = Path("deploy/server/deploy_release.sh").read_text(
        encoding="utf-8"
    )

    p5 = script.index(
        "-m simulation.webots.maze_car.tools.run_goal_acceptance"
    )
    switch = script.index('mv -Tf "${candidate_link}" "${current_link}"')
    assert p5 < switch
    assert (
        'goal_acceptance_root="/srv/maze/shared/acceptance/goal"'
        in script
    )


def test_truth_safety_is_evaluation_only_but_must_be_measured():
    map_definition = json.loads(
        Path(
            "simulation/webots/maze_car/config/maps/"
            "task12-public-v2.json"
        ).read_text(encoding="utf-8")
    )
    result = evaluate_truth_safety(
        [
            {
                "sim_truth": {
                    "x_mm": -900.0,
                    "y_mm": 900.0,
                    "yaw_deg": 0.0,
                    "collision_count": 0,
                }
            },
            {
                "sim_truth": {
                    "x_mm": -450.0,
                    "y_mm": 900.0,
                    "yaw_deg": 90.0,
                    "collision_count": 0,
                }
            },
        ],
        map_definition=map_definition,
    )

    assert result == {
        "truth_sample_count": 2,
        "collision_count": 0,
        "out_of_bounds_count": 0,
        "wall_crossing_count": 0,
    }
