from rdk_maze_tuner.core.slip_estimator import (
    SlipEstimator,
    SlipEstimatorConfig,
)


def estimator() -> SlipEstimator:
    return SlipEstimator(
        SlipEstimatorConfig(
            mm_per_tick=300.0 / 1350.0,
            wheel_base_mm=135.0,
        )
    )


def test_matching_external_motion_reports_low_slip_and_normal_profile():
    slip = estimator()
    slip.update(
        timestamp_ms=0,
        enc_left=0,
        enc_right=0,
        imu_available=True,
        imu_yaw_deg=0.0,
        external_distance_mm=0.0,
    )

    result = slip.update(
        timestamp_ms=1000,
        enc_left=450,
        enc_right=450,
        imu_available=True,
        imu_yaw_deg=0.0,
        external_distance_mm=100.0,
    )

    assert result.left_slip_rate < 0.02
    assert result.right_slip_rate < 0.02
    assert result.overall_slip_rate < 0.02
    assert result.friction_profile == "normal"
    assert result.equivalent_friction > 0.95
    assert result.quality == "good"


def test_encoder_motion_without_external_displacement_reports_wheelspin():
    slip = estimator()
    slip.update(
        timestamp_ms=0,
        enc_left=0,
        enc_right=0,
        imu_available=True,
        imu_yaw_deg=0.0,
        external_distance_mm=0.0,
    )

    result = slip.update(
        timestamp_ms=1000,
        enc_left=450,
        enc_right=450,
        imu_available=True,
        imu_yaw_deg=0.0,
        external_distance_mm=20.0,
    )

    assert result.left_slip_rate == 0.8
    assert result.right_slip_rate == 0.8
    assert result.overall_slip_rate == 0.8
    assert result.friction_profile == "low"
    assert result.equivalent_friction < 0.4
    assert "wheelspin_suspected" in result.quality_flags


def test_imu_yaw_evidence_can_identify_asymmetric_left_slip():
    slip = estimator()
    slip.update(
        timestamp_ms=0,
        enc_left=0,
        enc_right=0,
        imu_available=True,
        imu_yaw_deg=0.0,
        external_distance_mm=0.0,
    )

    result = slip.update(
        timestamp_ms=1000,
        enc_left=450,
        enc_right=225,
        imu_available=True,
        imu_yaw_deg=8.488,
        external_distance_mm=60.0,
    )

    assert result.left_slip_rate > 0.25
    assert result.right_slip_rate < 0.05
    assert result.left_slip_rate > result.right_slip_rate
    assert result.quality == "good"


def test_missing_external_evidence_is_unknown_not_physical_truth():
    slip = estimator()
    slip.update(
        timestamp_ms=0,
        enc_left=0,
        enc_right=0,
        imu_available=False,
        imu_yaw_deg=None,
        external_distance_mm=None,
    )

    result = slip.update(
        timestamp_ms=1000,
        enc_left=450,
        enc_right=450,
        imu_available=False,
        imu_yaw_deg=None,
        external_distance_mm=None,
    )

    assert result.left_slip_rate is None
    assert result.right_slip_rate is None
    assert result.overall_slip_rate is None
    assert result.equivalent_friction is None
    assert result.friction_profile == "unknown"
    assert result.quality == "insufficient"
    assert "insufficient_external_evidence" in result.quality_flags
    assert result.to_dict()["is_physical_truth"] is False
