from pathlib import Path
from math import nan

import pytest

from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")


def test_param_manager_loads_nested_values_and_flattens_esp32_params():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    assert manager.get("motor.base_speed") == 0.25
    assert manager.get("motion.cell_ticks") == 1350
    assert manager.esp32_params()["base_speed"] == 0.25
    assert manager.esp32_params()["cell_ticks"] == 1350
    assert manager.param_version == 1


def test_param_manager_rejects_out_of_range_update():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    try:
        manager.apply_updates({"motor.base_speed": 2.0}, source="test")
    except ParamValidationError as exc:
        assert "motor.base_speed" in str(exc)
    else:
        raise AssertionError("expected ParamValidationError")


def test_param_manager_records_valid_update_change_log():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    event = manager.apply_updates({"motor.base_speed": 0.3}, source="test")

    assert manager.get("motor.base_speed") == 0.3
    assert manager.param_version == 2
    assert event["type"] == "param_change"
    assert event["changes"]["motor.base_speed"] == [0.25, 0.3]


def test_arrival_verification_config_is_validated_and_not_exported_to_esp32():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    assert manager.arrival_verification_config().to_dict() == {
        "nominal_position_error_ratio": 0.10,
        "recoverable_position_error_ratio": 0.20,
        "nominal_heading_error_deg": 8.0,
        "recoverable_heading_error_deg": 12.0,
        "goal_min_confidence": 0.80,
        "max_recovery_attempts_per_cell": 2,
    }
    assert not any(
        key.startswith("arrival_verification")
        for key in manager.esp32_params()
    )


def test_arrival_verification_rejects_unknown_relation_and_non_finite_updates():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    with pytest.raises(ParamValidationError, match="not a known parameter"):
        manager.apply_updates(
            {"arrival_verification.unknown": 1},
            source="manual",
        )
    with pytest.raises(ParamValidationError, match="nominal.*recoverable"):
        manager.apply_updates(
            {"arrival_verification.nominal_position_error_ratio": 0.25},
            source="manual",
        )
    with pytest.raises(ParamValidationError, match="finite"):
        manager.apply_updates(
            {"arrival_verification.goal_min_confidence": nan},
            source="manual",
        )


def test_auto_tune_source_cannot_apply_arrival_verification_update():
    manager = ParamManager(params_path=PARAMS, limits_path=LIMITS)

    with pytest.raises(ParamValidationError, match="cannot modify"):
        manager.apply_updates(
            {"arrival_verification.goal_min_confidence": 0.85},
            source="auto_tune",
        )
