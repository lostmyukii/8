import dataclasses
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from simulation.webots.maze_car.physical_config import (
    DEFAULT_PHYSICAL_PROFILE_DIR,
    BodyConfig,
    EncoderConfig,
    GeometryConfig,
    ImuConfig,
    MotorConfig,
    PhysicalConfigError,
    PhysicalProfile,
    PhysicalProfileRepository,
    RuntimeConfig,
    SurfaceConfig,
    ToFConfig,
)


PROFILE_IDS = (
    "asymmetric-v1",
    "local-patch-v1",
    "low-v1",
    "normal-v1",
)


def _payload(profile_id: str = "normal-v1") -> dict:
    return yaml.safe_load(
        (DEFAULT_PHYSICAL_PROFILE_DIR / f"{profile_id}.yaml").read_text(
            encoding="utf-8"
        )
    )


def _write_profile(
    directory: Path,
    payload: dict,
    *,
    filename: str | None = None,
) -> Path:
    path = directory / (filename or f"{payload['profile_id']}.yaml")
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_normal_profile_loads_confirmed_physical_baseline():
    profile = PhysicalProfileRepository().get("normal-v1")

    assert isinstance(profile, PhysicalProfile)
    assert isinstance(profile.geometry, GeometryConfig)
    assert isinstance(profile.body, BodyConfig)
    assert isinstance(profile.motor, MotorConfig)
    assert isinstance(profile.encoder, EncoderConfig)
    assert isinstance(profile.tof, ToFConfig)
    assert isinstance(profile.imu, ImuConfig)
    assert isinstance(profile.surface, SurfaceConfig)
    assert isinstance(profile.runtime, RuntimeConfig)
    assert profile.geometry.wheel_radius_m == pytest.approx(0.0325)
    assert profile.geometry.axle_track_m == pytest.approx(0.135)
    assert profile.body.total_mass_kg == pytest.approx(1.20)
    assert profile.body.body_mass_kg == pytest.approx(1.08)
    assert profile.body.wheel_mass_kg == pytest.approx(0.06)
    assert profile.encoder.ticks_per_revolution == 1103
    assert profile.runtime.basic_time_step_ms == 8
    assert profile.runtime.telemetry_period_ms == 50
    assert profile.runtime.render_fps == 24
    assert profile.random_seed == 20260801
    assert len(profile.digest) == 64
    assert profile.digest == hashlib.sha256(
        profile.canonical_json.encode("utf-8")
    ).hexdigest()


def test_profiles_are_frozen_and_snapshots_do_not_mutate_loaded_values():
    profile = PhysicalProfileRepository().get("normal-v1")

    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.random_seed = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.geometry.wheel_radius_m = 1.0

    snapshot = profile.to_dict()
    snapshot["geometry"]["wheel_radius_m"] = 1.0
    snapshot["body"]["center_of_mass_m"][0] = 9.0

    assert profile.geometry.wheel_radius_m == pytest.approx(0.0325)
    assert profile.body.center_of_mass_m == (0.0, 0.07, 0.01)


def test_repository_lists_four_profiles_with_only_surface_differences():
    profiles = PhysicalProfileRepository().list_profiles()

    assert tuple(profile.profile_id for profile in profiles) == PROFILE_IDS
    assert {profile.random_seed for profile in profiles} == {20260801}

    common_snapshots = []
    for profile in profiles:
        snapshot = profile.to_dict()
        snapshot.pop("profile_id")
        snapshot.pop("surface")
        common_snapshots.append(snapshot)
    assert common_snapshots.count(common_snapshots[0]) == len(
        common_snapshots
    )

    by_id = {profile.profile_id: profile for profile in profiles}
    assert by_id["normal-v1"].surface.profile == "normal"
    assert by_id["normal-v1"].surface.left_wheel_friction == pytest.approx(
        0.90
    )
    assert by_id["normal-v1"].surface.right_wheel_friction == pytest.approx(
        0.90
    )
    assert by_id["low-v1"].surface.left_wheel_friction == pytest.approx(0.25)
    assert by_id["low-v1"].surface.right_wheel_friction == pytest.approx(
        0.25
    )
    assert by_id[
        "asymmetric-v1"
    ].surface.left_wheel_friction == pytest.approx(0.35)
    assert by_id[
        "asymmetric-v1"
    ].surface.right_wheel_friction == pytest.approx(0.90)
    assert by_id["local-patch-v1"].surface.patch_enabled is True
    assert by_id["local-patch-v1"].surface.patch_friction == pytest.approx(
        0.25
    )


def test_digest_is_canonical_across_yaml_key_order_and_formatting(tmp_path):
    payload = _payload()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    _write_profile(first_dir, payload)

    reordered = {
        key: payload[key]
        for key in reversed(tuple(payload))
    }
    (second_dir / "normal-v1.yaml").write_text(
        yaml.safe_dump(
            reordered,
            allow_unicode=True,
            default_flow_style=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    first = PhysicalProfileRepository(first_dir).get("normal-v1")
    second = PhysicalProfileRepository(second_dir).get("normal-v1")

    assert first.canonical_json == second.canonical_json
    assert first.digest == second.digest
    assert json.loads(first.canonical_json) == first.to_dict()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["geometry"].__setitem__(
                "wheel_radius_m", -0.1
            ),
            "geometry.wheel_radius_m",
        ),
        (
            lambda value: value["body"].__setitem__("total_mass_kg", -1),
            "body.total_mass_kg",
        ),
        (
            lambda value: value["motor"].__setitem__(
                "pwm_dead_zone", 1.0
            ),
            "motor.pwm_dead_zone",
        ),
        (
            lambda value: value["encoder"].__setitem__(
                "missed_pulse_rate", 1.0
            ),
            "encoder.missed_pulse_rate",
        ),
        (
            lambda value: value["tof"].__setitem__("dropout_rate", -0.1),
            "tof.dropout_rate",
        ),
        (
            lambda value: value["surface"].__setitem__(
                "left_wheel_friction", -0.1
            ),
            "surface.left_wheel_friction",
        ),
        (
            lambda value: value["runtime"].__setitem__(
                "basic_time_step_ms", 16
            ),
            "runtime.basic_time_step_ms",
        ),
    ],
)
def test_rejects_out_of_range_values(tmp_path, mutate, message):
    payload = _payload()
    mutate(payload)
    _write_profile(tmp_path, payload)

    with pytest.raises(PhysicalConfigError, match=message):
        PhysicalProfileRepository(tmp_path).get("normal-v1")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["body"].__setitem__(
                "center_of_mass_m", [0.0, float("nan"), 0.01]
            ),
            "body.center_of_mass_m",
        ),
        (
            lambda value: value["body"].__setitem__(
                "inertia_matrix_kg_m2",
                [0.01, 0.01, float("inf"), 0.0, 0.0, 0.0],
            ),
            "body.inertia_matrix_kg_m2",
        ),
        (
            lambda value: value["motor"].__setitem__(
                "max_torque_nm", float("nan")
            ),
            "motor.max_torque_nm",
        ),
    ],
)
def test_rejects_non_finite_values(tmp_path, mutate, message):
    payload = _payload()
    mutate(payload)
    _write_profile(tmp_path, payload)

    with pytest.raises(PhysicalConfigError, match=message):
        PhysicalProfileRepository(tmp_path).get("normal-v1")


def test_rejects_inconsistent_total_mass(tmp_path):
    payload = _payload()
    payload["body"]["body_mass_kg"] = 1.0
    _write_profile(tmp_path, payload)

    with pytest.raises(PhysicalConfigError, match="total_mass_kg"):
        PhysicalProfileRepository(tmp_path).get("normal-v1")


def test_rejects_missing_and_unknown_fields(tmp_path):
    missing = _payload()
    del missing["tof"]["noise_std_mm"]
    _write_profile(tmp_path, missing)

    with pytest.raises(PhysicalConfigError, match="tof.noise_std_mm"):
        PhysicalProfileRepository(tmp_path).get("normal-v1")

    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()
    unknown = _payload()
    unknown["motor"]["secret_boost"] = 2
    _write_profile(unknown_dir, unknown)

    with pytest.raises(PhysicalConfigError, match="motor.secret_boost"):
        PhysicalProfileRepository(unknown_dir).get("normal-v1")


def test_rejects_profile_name_mismatch_path_traversal_and_digest_mismatch(
    tmp_path,
):
    payload = _payload()
    payload["profile_id"] = "other-v1"
    _write_profile(tmp_path, payload, filename="normal-v1.yaml")
    repository = PhysicalProfileRepository(tmp_path)

    with pytest.raises(PhysicalConfigError, match="profile_id"):
        repository.get("normal-v1")
    with pytest.raises(PhysicalConfigError, match="profile_id"):
        repository.get("../normal-v1")

    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    valid_payload = _payload()
    _write_profile(valid_dir, valid_payload)
    valid_repository = PhysicalProfileRepository(valid_dir)
    with pytest.raises(PhysicalConfigError, match="digest"):
        valid_repository.get("normal-v1", expected_digest="0" * 64)


def test_rejects_duplicate_profile_ids(tmp_path):
    payload = _payload()
    _write_profile(tmp_path, payload)
    duplicate = deepcopy(payload)
    _write_profile(tmp_path, duplicate, filename="duplicate.yaml")

    with pytest.raises(PhysicalConfigError, match="duplicate profile_id"):
        PhysicalProfileRepository(tmp_path).list_profiles()
