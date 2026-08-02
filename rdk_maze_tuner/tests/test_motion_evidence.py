from __future__ import annotations

from dataclasses import replace
from math import nan

import pytest

from rdk_maze_tuner.core.motion_evidence import (
    MOTION_EVIDENCE_UNSAFE,
    POSE_UNCERTAIN,
    WHEEL_SLIP_DETECTED,
    ArrivalVerificationConfig,
    MotionEvidenceGate,
    MotionEvidenceInput,
)


def evidence(**overrides) -> MotionEvidenceInput:
    values = {
        "action_id": "move-0001",
        "action_name": "move_cell",
        "expected_distance_mm": 450.0,
        "measured_distance_mm": 445.0,
        "encoder_displacement_mm": 448.0,
        "external_displacement_mm": 445.0,
        "expected_heading_deg": 0.0,
        "measured_heading_deg": 2.0,
        "pose_confidence": 0.90,
        "recovery_attempts": 0,
        "correction_evidence_available": True,
    }
    values.update(overrides)
    return MotionEvidenceInput(**values)


def test_arrival_verification_defaults_are_frozen_and_exact():
    config = ArrivalVerificationConfig()

    assert config.to_dict() == {
        "nominal_position_error_ratio": 0.10,
        "recoverable_position_error_ratio": 0.20,
        "nominal_heading_error_deg": 8.0,
        "recoverable_heading_error_deg": 12.0,
        "goal_min_confidence": 0.80,
        "max_recovery_attempts_per_cell": 2,
    }
    with pytest.raises(Exception):
        config.goal_min_confidence = 0.1


def test_nominal_position_and_heading_evidence_is_accepted():
    decision = MotionEvidenceGate().evaluate(
        evidence(
            measured_distance_mm=405.0,
            measured_heading_deg=8.0,
        )
    )

    assert decision.status == "accepted"
    assert decision.code is None
    assert decision.position_error_ratio == pytest.approx(0.10)
    assert decision.heading_error_deg == pytest.approx(8.0)
    assert decision.recovery is None


def test_450_to_365_mm_is_recoverable_not_accepted():
    decision = MotionEvidenceGate().evaluate(
        evidence(
            measured_distance_mm=365.0,
            external_displacement_mm=365.0,
            measured_heading_deg=9.0,
        )
    )

    assert decision.status == "recoverable"
    assert decision.position_error_ratio == pytest.approx(85 / 450)
    assert decision.recovery is not None
    assert decision.recovery.kind == "nudge_forward"
    assert decision.recovery.remaining_distance_mm == pytest.approx(85.0)


def test_position_or_heading_beyond_recoverable_limit_is_unsafe():
    gate = MotionEvidenceGate()

    position = gate.evaluate(
        evidence(
            measured_distance_mm=359.0,
            external_displacement_mm=359.0,
        )
    )
    heading_at_boundary = gate.evaluate(
        evidence(measured_distance_mm=405.0, measured_heading_deg=12.0)
    )
    heading_over_boundary = gate.evaluate(
        evidence(
            measured_distance_mm=405.0,
            measured_heading_deg=12.0001,
        )
    )

    assert position.status == "unsafe"
    assert position.code == MOTION_EVIDENCE_UNSAFE
    assert heading_at_boundary.status == "recoverable"
    assert heading_at_boundary.recovery.kind == "align_heading"
    assert heading_over_boundary.status == "unsafe"


def test_encoder_motion_without_external_motion_is_wheel_slip():
    decision = MotionEvidenceGate().evaluate(
        evidence(
            encoder_displacement_mm=450.0,
            external_displacement_mm=0.5,
        )
    )

    assert decision.status == "unsafe"
    assert decision.code == WHEEL_SLIP_DETECTED


def test_low_pose_confidence_is_unsafe():
    decision = MotionEvidenceGate().evaluate(
        evidence(pose_confidence=0.7999)
    )

    assert decision.status == "unsafe"
    assert decision.code == POSE_UNCERTAIN


def test_sim_truth_is_not_an_algorithm_input():
    payload = evidence().__dict__
    first = MotionEvidenceInput.from_mapping(
        {**payload, "sim_truth": {"x_mm": 0, "yaw_deg": 0}}
    )
    second = MotionEvidenceInput.from_mapping(
        {**payload, "sim_truth": {"x_mm": 999999, "yaw_deg": 179}}
    )

    assert not hasattr(first, "sim_truth")
    assert MotionEvidenceGate().evaluate(first) == (
        MotionEvidenceGate().evaluate(second)
    )


def test_config_rejects_unknown_invalid_and_non_finite_values():
    valid = ArrivalVerificationConfig().to_dict()

    with pytest.raises(ValueError, match="unknown"):
        ArrivalVerificationConfig.from_mapping({**valid, "extra": 1})
    with pytest.raises(ValueError, match="nominal.*recoverable"):
        ArrivalVerificationConfig.from_mapping(
            {
                **valid,
                "nominal_position_error_ratio": 0.30,
                "recoverable_position_error_ratio": 0.20,
            }
        )
    with pytest.raises(ValueError, match="finite"):
        ArrivalVerificationConfig.from_mapping(
            {**valid, "goal_min_confidence": nan}
        )


def test_recovery_limit_blocks_more_recovery_but_not_final_acceptance():
    config = ArrivalVerificationConfig(max_recovery_attempts_per_cell=2)
    gate = MotionEvidenceGate(config)

    unsafe = gate.evaluate(
        evidence(
            measured_distance_mm=365.0,
            external_displacement_mm=365.0,
            recovery_attempts=2,
        )
    )
    accepted = gate.evaluate(
        replace(
            evidence(),
            recovery_attempts=2,
        )
    )

    assert unsafe.status == "unsafe"
    assert unsafe.code == "MOTION_RECOVERY_FAILED"
    assert accepted.status == "accepted"
