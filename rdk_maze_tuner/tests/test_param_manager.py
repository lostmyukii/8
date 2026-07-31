from pathlib import Path

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

