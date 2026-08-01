from dataclasses import FrozenInstanceError
import inspect

import pytest

from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)
from simulation.webots.maze_car.physical_scenarios import (
    PhysicalScenarioError,
    PhysicalScenarioRepository,
    evaluate_scenario_frames,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.action_controller import (
    ActionRequest,
    PhysicalActionController,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.truth_observer import (
    compute_truth_slip_rate,
)


EXPECTED = {
    "normal-p3-v1": "normal-v1",
    "low-p4-v1": "low-v1",
    "asymmetric-p4-v1": "asymmetric-v1",
    "local-patch-p4-v1": "local-patch-v1",
}


def test_repository_loads_four_frozen_digest_bound_scenarios():
    repository = PhysicalScenarioRepository()

    assert {
        item.scenario_id: item.physical_profile_id
        for item in repository.list_scenarios()
    } == EXPECTED
    for scenario_id, profile_id in EXPECTED.items():
        scenario = repository.get(scenario_id)
        profile = PhysicalProfileRepository().get(profile_id)
        assert scenario.physical_profile_digest == profile.digest
        assert scenario.seed == profile.random_seed == 20260801
        assert scenario.actions
        assert scenario.expected_observations
        assert scenario.acceptance_thresholds
        assert scenario.timeout_ms > 0
        assert len(scenario.digest) == 64
        with pytest.raises(FrozenInstanceError):
            scenario.seed = 1


def test_scenario_profile_binding_cannot_be_overridden_at_runtime():
    repository = PhysicalScenarioRepository()

    profile = repository.resolve_profile(
        "low-p4-v1",
        profiles=PhysicalProfileRepository(),
    )

    assert profile.profile_id == "low-v1"
    with pytest.raises(PhysicalScenarioError, match="bound"):
        repository.resolve_profile(
            "low-p4-v1",
            profiles=PhysicalProfileRepository(),
            requested_profile_id="normal-v1",
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["scenarios"].append(
            dict(payload["scenarios"][0])
        ),
        lambda payload: payload["scenarios"][0].update(
            {"unknown": True}
        ),
        lambda payload: payload["scenarios"][0].update(
            {"seed": -1}
        ),
        lambda payload: payload["scenarios"][0].update(
            {"world": "../../unsafe.wbt"}
        ),
        lambda payload: payload["scenarios"][0].update(
            {"physical_profile_digest": "0" * 64}
        ),
    ],
)
def test_invalid_or_ambiguous_scenario_payload_is_rejected(mutator):
    payload = PhysicalScenarioRepository().snapshot()
    mutator(payload)

    with pytest.raises(PhysicalScenarioError):
        PhysicalScenarioRepository.from_payload(payload)


def test_structured_metrics_expose_slip_surface_and_encoder_truth_gap():
    frames = [
        {
            "enc_left": 0,
            "enc_right": 0,
            "wheel_speed_left_rad_s": 0.0,
            "wheel_speed_right_rad_s": 0.0,
            "sim_truth": {
                "x_mm": 0,
                "y_mm": 0,
                "yaw_deg": 0,
                "left_slip_rate": 0.02,
                "right_slip_rate": 0.03,
                "active_surface": "normal",
                "collision_count": 0,
            },
        },
        {
            "enc_left": 1350,
            "enc_right": 1340,
            "wheel_speed_left_rad_s": 3.0,
            "wheel_speed_right_rad_s": 2.0,
            "sim_truth": {
                "x_mm": 8,
                "y_mm": -238,
                "yaw_deg": 2,
                "left_slip_rate": 0.18,
                "right_slip_rate": 0.10,
                "active_surface": "local_patch",
                "collision_count": 1,
            },
        },
    ]

    metrics = evaluate_scenario_frames(
        frames,
        ticks_per_mm=5.4,
        completed_actions=1,
        requested_actions=1,
    )

    assert metrics.truth_distance_mm == pytest.approx(238.1344, rel=1e-4)
    assert metrics.encoder_distance_mm == pytest.approx(
        1345 / 5.4
    )
    assert metrics.encoder_truth_gap_mm > 10
    assert metrics.mean_abs_left_slip == pytest.approx(0.18)
    assert metrics.mean_abs_right_slip == pytest.approx(0.10)
    assert metrics.mean_abs_slip_difference == pytest.approx(0.08)
    assert metrics.surface_transitions == 1
    assert metrics.collision_count == 1
    assert metrics.success_rate == 1.0
    assert metrics.to_dict()["final_yaw_deg"] == 2.0


def test_stopped_frames_do_not_inflate_slip_metrics():
    frames = [
        {
            "enc_left": 0,
            "enc_right": 0,
            "wheel_speed_left_rad_s": 0.0,
            "wheel_speed_right_rad_s": 0.0,
            "sim_truth": {
                "x_mm": 0,
                "y_mm": 0,
                "yaw_deg": 0,
                "left_slip_rate": 99.0,
                "right_slip_rate": -99.0,
                "active_surface": "normal",
                "collision_count": 0,
            },
        },
        {
            "enc_left": 54,
            "enc_right": 54,
            "wheel_speed_left_rad_s": 1.0,
            "wheel_speed_right_rad_s": 1.0,
            "sim_truth": {
                "x_mm": 0,
                "y_mm": -8,
                "yaw_deg": 0,
                "left_slip_rate": 0.2,
                "right_slip_rate": 0.1,
                "active_surface": "normal",
                "collision_count": 0,
            },
        },
    ]

    metrics = evaluate_scenario_frames(
        frames,
        ticks_per_mm=5.4,
        completed_actions=1,
        requested_actions=1,
    )

    assert metrics.mean_abs_left_slip == pytest.approx(0.2)
    assert metrics.mean_abs_right_slip == pytest.approx(0.1)
    assert metrics.mean_abs_slip_difference == pytest.approx(0.1)


def test_truth_slip_uses_signed_wheel_surface_and_longitudinal_speed():
    assert compute_truth_slip_rate(2.0, 0.5) == 0.75
    assert compute_truth_slip_rate(-2.0, -0.5) == -0.75
    assert compute_truth_slip_rate(0.0, 0.0) == 0.0


def test_control_contract_has_no_truth_sample_input():
    assert "truth" not in inspect.signature(
        PhysicalActionController.tick
    ).parameters
    assert "truth" not in {
        field.name
        for field in __import__("dataclasses").fields(ActionRequest)
    }
    source = inspect.getsource(PhysicalActionController)
    assert "TruthSample" not in source
    assert "sim_truth" not in source
