"""Strict, immutable physical profiles for the Webots maze car."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


DEFAULT_PHYSICAL_PROFILE_DIR = (
    Path(__file__).resolve().parent / "config" / "physical_profiles"
)
_PROFILE_ID_PATTERN = re.compile(
    r"^(?:normal|low|asymmetric|local-patch)-v[1-9][0-9]*$"
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "random_seed",
        "geometry",
        "body",
        "motor",
        "encoder",
        "tof",
        "imu",
        "surface",
        "runtime",
    }
)


class PhysicalConfigError(ValueError):
    """Raised when a physical profile is missing, unsafe, or ambiguous."""


@dataclass(frozen=True)
class GeometryConfig:
    wheel_radius_m: float
    wheel_width_m: float
    axle_track_m: float
    chassis_length_m: float
    chassis_width_m: float


@dataclass(frozen=True)
class BodyConfig:
    total_mass_kg: float
    body_mass_kg: float
    wheel_mass_kg: float
    center_of_mass_m: tuple[float, float, float]
    inertia_matrix_kg_m2: tuple[
        float,
        float,
        float,
        float,
        float,
        float,
    ]


@dataclass(frozen=True)
class MotorConfig:
    max_velocity_rad_s: float
    max_torque_nm: float
    response_time_s: float
    pwm_dead_zone: float
    left_gain: float
    right_gain: float


@dataclass(frozen=True)
class EncoderConfig:
    ticks_per_revolution: int
    quantization_enabled: bool
    missed_pulse_rate: float


@dataclass(frozen=True)
class ToFConfig:
    min_range_m: float
    max_range_m: float
    field_of_view_deg: float
    noise_std_mm: float
    dropout_rate: float


@dataclass(frozen=True)
class ImuConfig:
    yaw_noise_std_deg: float
    gyro_noise_std_dps: float
    accel_noise_std_mps2: float


@dataclass(frozen=True)
class SurfaceConfig:
    profile: str
    base_floor_friction: float
    left_wheel_friction: float
    right_wheel_friction: float
    patch_enabled: bool
    patch_friction: float


@dataclass(frozen=True)
class RuntimeConfig:
    basic_time_step_ms: int
    telemetry_period_ms: int
    render_fps: int


@dataclass(frozen=True)
class PhysicalProfile:
    schema_version: int
    profile_id: str
    random_seed: int
    geometry: GeometryConfig
    body: BodyConfig
    motor: MotorConfig
    encoder: EncoderConfig
    tof: ToFConfig
    imu: ImuConfig
    surface: SurfaceConfig
    runtime: RuntimeConfig

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible snapshot."""

        return _json_ready(asdict(self))

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


class PhysicalProfileRepository:
    """Read immutable profiles from one controlled directory."""

    def __init__(
        self,
        directory: Path | str = DEFAULT_PHYSICAL_PROFILE_DIR,
    ) -> None:
        self.directory = Path(directory)

    def get(
        self,
        profile_id: str,
        *,
        expected_digest: str | None = None,
    ) -> PhysicalProfile:
        _validate_profile_id(profile_id)
        path = self._controlled_file(f"{profile_id}.yaml")
        profile = _load_profile(path)
        if profile.profile_id != profile_id:
            raise PhysicalConfigError(
                f"{path}: profile_id {profile.profile_id!r} does not match "
                f"requested profile_id {profile_id!r}"
            )
        if (
            expected_digest is not None
            and profile.digest != expected_digest
        ):
            raise PhysicalConfigError(
                f"{path}: digest mismatch for profile_id {profile_id!r}"
            )
        return profile

    def list_profiles(self) -> tuple[PhysicalProfile, ...]:
        root = self._controlled_root()
        paths = sorted(root.glob("*.yaml"))
        profiles: list[tuple[Path, PhysicalProfile]] = []
        seen: set[str] = set()
        for path in paths:
            controlled = self._ensure_within_root(path)
            profile = _load_profile(controlled)
            if profile.profile_id in seen:
                raise PhysicalConfigError(
                    f"{controlled}: duplicate profile_id "
                    f"{profile.profile_id!r}"
                )
            seen.add(profile.profile_id)
            profiles.append((controlled, profile))
        for path, profile in profiles:
            if path.stem != profile.profile_id:
                raise PhysicalConfigError(
                    f"{path}: profile_id {profile.profile_id!r} does not "
                    f"match filename {path.stem!r}"
                )
        return tuple(
            sorted(
                (profile for _path, profile in profiles),
                key=lambda profile: profile.profile_id,
            )
        )

    def _controlled_root(self) -> Path:
        try:
            root = self.directory.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PhysicalConfigError(
                f"physical profile directory does not exist: "
                f"{self.directory}"
            ) from exc
        if not root.is_dir():
            raise PhysicalConfigError(
                f"physical profile path is not a directory: {root}"
            )
        return root

    def _controlled_file(self, filename: str) -> Path:
        root = self._controlled_root()
        path = root / filename
        try:
            return self._ensure_within_root(path)
        except FileNotFoundError as exc:
            raise PhysicalConfigError(
                f"physical profile does not exist: {filename.removesuffix('.yaml')}"
            ) from exc

    def _ensure_within_root(self, path: Path) -> Path:
        root = self._controlled_root()
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise PhysicalConfigError(
                f"physical profile escapes controlled directory: {path}"
            )
        if not resolved.is_file():
            raise PhysicalConfigError(
                f"physical profile is not a file: {path}"
            )
        return resolved


def _load_profile(path: Path) -> PhysicalProfile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PhysicalConfigError(
            f"{path}: cannot read physical profile: {exc}"
        ) from exc
    payload = _strict_mapping(raw, "profile", _TOP_LEVEL_FIELDS)

    schema_version = _integer(
        payload["schema_version"],
        "schema_version",
        minimum=1,
        maximum=1,
    )
    profile_id = _text(payload["profile_id"], "profile_id")
    _validate_profile_id(profile_id)
    random_seed = _integer(
        payload["random_seed"],
        "random_seed",
        minimum=0,
        maximum=2_147_483_647,
    )

    geometry_payload = _strict_mapping(
        payload["geometry"],
        "geometry",
        frozenset(
            {
                "wheel_radius_m",
                "wheel_width_m",
                "axle_track_m",
                "chassis_length_m",
                "chassis_width_m",
            }
        ),
    )
    geometry = GeometryConfig(
        wheel_radius_m=_number(
            geometry_payload["wheel_radius_m"],
            "geometry.wheel_radius_m",
            minimum=0.005,
            maximum=0.2,
        ),
        wheel_width_m=_number(
            geometry_payload["wheel_width_m"],
            "geometry.wheel_width_m",
            minimum=0.005,
            maximum=0.2,
        ),
        axle_track_m=_number(
            geometry_payload["axle_track_m"],
            "geometry.axle_track_m",
            minimum=0.02,
            maximum=1.0,
        ),
        chassis_length_m=_number(
            geometry_payload["chassis_length_m"],
            "geometry.chassis_length_m",
            minimum=0.02,
            maximum=2.0,
        ),
        chassis_width_m=_number(
            geometry_payload["chassis_width_m"],
            "geometry.chassis_width_m",
            minimum=0.02,
            maximum=2.0,
        ),
    )

    body_payload = _strict_mapping(
        payload["body"],
        "body",
        frozenset(
            {
                "total_mass_kg",
                "body_mass_kg",
                "wheel_mass_kg",
                "center_of_mass_m",
                "inertia_matrix_kg_m2",
            }
        ),
    )
    center_of_mass = _number_tuple(
        body_payload["center_of_mass_m"],
        "body.center_of_mass_m",
        length=3,
        minimum=-2.0,
        maximum=2.0,
    )
    inertia = _inertia_tuple(
        body_payload["inertia_matrix_kg_m2"],
        "body.inertia_matrix_kg_m2",
    )
    body = BodyConfig(
        total_mass_kg=_number(
            body_payload["total_mass_kg"],
            "body.total_mass_kg",
            minimum=0.01,
            maximum=100.0,
        ),
        body_mass_kg=_number(
            body_payload["body_mass_kg"],
            "body.body_mass_kg",
            minimum=0.001,
            maximum=100.0,
        ),
        wheel_mass_kg=_number(
            body_payload["wheel_mass_kg"],
            "body.wheel_mass_kg",
            minimum=0.001,
            maximum=10.0,
        ),
        center_of_mass_m=(
            center_of_mass[0],
            center_of_mass[1],
            center_of_mass[2],
        ),
        inertia_matrix_kg_m2=(
            inertia[0],
            inertia[1],
            inertia[2],
            inertia[3],
            inertia[4],
            inertia[5],
        ),
    )
    expected_mass = body.body_mass_kg + 2.0 * body.wheel_mass_kg
    if not math.isclose(
        body.total_mass_kg,
        expected_mass,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise PhysicalConfigError(
            "body.total_mass_kg must equal body.body_mass_kg plus twice "
            "body.wheel_mass_kg"
        )

    motor_payload = _strict_mapping(
        payload["motor"],
        "motor",
        frozenset(
            {
                "max_velocity_rad_s",
                "max_torque_nm",
                "response_time_s",
                "pwm_dead_zone",
                "left_gain",
                "right_gain",
            }
        ),
    )
    motor = MotorConfig(
        max_velocity_rad_s=_number(
            motor_payload["max_velocity_rad_s"],
            "motor.max_velocity_rad_s",
            minimum=0.1,
            maximum=100.0,
        ),
        max_torque_nm=_number(
            motor_payload["max_torque_nm"],
            "motor.max_torque_nm",
            minimum=0.001,
            maximum=10.0,
        ),
        response_time_s=_number(
            motor_payload["response_time_s"],
            "motor.response_time_s",
            minimum=0.001,
            maximum=5.0,
        ),
        pwm_dead_zone=_number(
            motor_payload["pwm_dead_zone"],
            "motor.pwm_dead_zone",
            minimum=0.0,
            maximum=1.0,
            maximum_inclusive=False,
        ),
        left_gain=_number(
            motor_payload["left_gain"],
            "motor.left_gain",
            minimum=0.01,
            maximum=5.0,
        ),
        right_gain=_number(
            motor_payload["right_gain"],
            "motor.right_gain",
            minimum=0.01,
            maximum=5.0,
        ),
    )

    encoder_payload = _strict_mapping(
        payload["encoder"],
        "encoder",
        frozenset(
            {
                "ticks_per_revolution",
                "quantization_enabled",
                "missed_pulse_rate",
            }
        ),
    )
    encoder = EncoderConfig(
        ticks_per_revolution=_integer(
            encoder_payload["ticks_per_revolution"],
            "encoder.ticks_per_revolution",
            minimum=1,
            maximum=10_000_000,
        ),
        quantization_enabled=_boolean(
            encoder_payload["quantization_enabled"],
            "encoder.quantization_enabled",
        ),
        missed_pulse_rate=_number(
            encoder_payload["missed_pulse_rate"],
            "encoder.missed_pulse_rate",
            minimum=0.0,
            maximum=1.0,
            maximum_inclusive=False,
        ),
    )

    tof_payload = _strict_mapping(
        payload["tof"],
        "tof",
        frozenset(
            {
                "min_range_m",
                "max_range_m",
                "field_of_view_deg",
                "noise_std_mm",
                "dropout_rate",
            }
        ),
    )
    tof = ToFConfig(
        min_range_m=_number(
            tof_payload["min_range_m"],
            "tof.min_range_m",
            minimum=0.001,
            maximum=10.0,
        ),
        max_range_m=_number(
            tof_payload["max_range_m"],
            "tof.max_range_m",
            minimum=0.002,
            maximum=20.0,
        ),
        field_of_view_deg=_number(
            tof_payload["field_of_view_deg"],
            "tof.field_of_view_deg",
            minimum=0.1,
            maximum=180.0,
        ),
        noise_std_mm=_number(
            tof_payload["noise_std_mm"],
            "tof.noise_std_mm",
            minimum=0.0,
            maximum=1000.0,
        ),
        dropout_rate=_number(
            tof_payload["dropout_rate"],
            "tof.dropout_rate",
            minimum=0.0,
            maximum=1.0,
            maximum_inclusive=False,
        ),
    )
    if tof.min_range_m >= tof.max_range_m:
        raise PhysicalConfigError(
            "tof.min_range_m must be less than tof.max_range_m"
        )

    imu_payload = _strict_mapping(
        payload["imu"],
        "imu",
        frozenset(
            {
                "yaw_noise_std_deg",
                "gyro_noise_std_dps",
                "accel_noise_std_mps2",
            }
        ),
    )
    imu = ImuConfig(
        yaw_noise_std_deg=_number(
            imu_payload["yaw_noise_std_deg"],
            "imu.yaw_noise_std_deg",
            minimum=0.0,
            maximum=180.0,
        ),
        gyro_noise_std_dps=_number(
            imu_payload["gyro_noise_std_dps"],
            "imu.gyro_noise_std_dps",
            minimum=0.0,
            maximum=10_000.0,
        ),
        accel_noise_std_mps2=_number(
            imu_payload["accel_noise_std_mps2"],
            "imu.accel_noise_std_mps2",
            minimum=0.0,
            maximum=1000.0,
        ),
    )

    surface_payload = _strict_mapping(
        payload["surface"],
        "surface",
        frozenset(
            {
                "profile",
                "base_floor_friction",
                "left_wheel_friction",
                "right_wheel_friction",
                "patch_enabled",
                "patch_friction",
            }
        ),
    )
    surface = SurfaceConfig(
        profile=_text(surface_payload["profile"], "surface.profile"),
        base_floor_friction=_number(
            surface_payload["base_floor_friction"],
            "surface.base_floor_friction",
            minimum=0.0,
            maximum=5.0,
        ),
        left_wheel_friction=_number(
            surface_payload["left_wheel_friction"],
            "surface.left_wheel_friction",
            minimum=0.0,
            maximum=5.0,
        ),
        right_wheel_friction=_number(
            surface_payload["right_wheel_friction"],
            "surface.right_wheel_friction",
            minimum=0.0,
            maximum=5.0,
        ),
        patch_enabled=_boolean(
            surface_payload["patch_enabled"],
            "surface.patch_enabled",
        ),
        patch_friction=_number(
            surface_payload["patch_friction"],
            "surface.patch_friction",
            minimum=0.0,
            maximum=5.0,
        ),
    )
    _validate_surface(profile_id, surface)

    runtime_payload = _strict_mapping(
        payload["runtime"],
        "runtime",
        frozenset(
            {
                "basic_time_step_ms",
                "telemetry_period_ms",
                "render_fps",
            }
        ),
    )
    runtime = RuntimeConfig(
        basic_time_step_ms=_integer(
            runtime_payload["basic_time_step_ms"],
            "runtime.basic_time_step_ms",
            minimum=8,
            maximum=8,
        ),
        telemetry_period_ms=_integer(
            runtime_payload["telemetry_period_ms"],
            "runtime.telemetry_period_ms",
            minimum=50,
            maximum=50,
        ),
        render_fps=_integer(
            runtime_payload["render_fps"],
            "runtime.render_fps",
            minimum=24,
            maximum=24,
        ),
    )

    return PhysicalProfile(
        schema_version=schema_version,
        profile_id=profile_id,
        random_seed=random_seed,
        geometry=geometry,
        body=body,
        motor=motor,
        encoder=encoder,
        tof=tof,
        imu=imu,
        surface=surface,
        runtime=runtime,
    )


def _strict_mapping(
    value: Any,
    path: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PhysicalConfigError(f"{path} must be an object")
    keys = {str(key) for key in value}
    missing = sorted(fields - keys)
    unknown = sorted(keys - fields)
    if missing:
        missing_path = (
            missing[0]
            if path == "profile"
            else f"{path}.{missing[0]}"
        )
        raise PhysicalConfigError(f"missing field: {missing_path}")
    if unknown:
        unknown_path = (
            unknown[0]
            if path == "profile"
            else f"{path}.{unknown[0]}"
        )
        raise PhysicalConfigError(f"unknown field: {unknown_path}")
    return {str(key): item for key, item in value.items()}


def _number(
    value: Any,
    path: str,
    *,
    minimum: float,
    maximum: float,
    maximum_inclusive: bool = True,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PhysicalConfigError(f"{path} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise PhysicalConfigError(f"{path} must be finite")
    above_maximum = (
        number > maximum
        if maximum_inclusive
        else number >= maximum
    )
    if number < minimum or above_maximum:
        closing = "]" if maximum_inclusive else ")"
        raise PhysicalConfigError(
            f"{path}={number!r} outside [{minimum}, {maximum}{closing}"
        )
    return number


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhysicalConfigError(f"{path} must be an integer")
    if value < minimum or value > maximum:
        raise PhysicalConfigError(
            f"{path}={value!r} outside [{minimum}, {maximum}]"
        )
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise PhysicalConfigError(f"{path} must be a boolean")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise PhysicalConfigError(f"{path} must be non-empty text")
    return value


def _number_tuple(
    value: Any,
    path: str,
    *,
    length: int,
    minimum: float,
    maximum: float,
) -> tuple[float, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != length
    ):
        raise PhysicalConfigError(
            f"{path} must contain exactly {length} numbers"
        )
    return tuple(
        _number(
            item,
            f"{path}[{index}]",
            minimum=minimum,
            maximum=maximum,
        )
        for index, item in enumerate(value)
    )


def _inertia_tuple(value: Any, path: str) -> tuple[float, ...]:
    values = _number_tuple(
        value,
        path,
        length=6,
        minimum=-10.0,
        maximum=10.0,
    )
    if any(item <= 0.0 for item in values[:3]):
        raise PhysicalConfigError(
            f"{path} diagonal values must be positive"
        )
    ixx, iyy, izz, ixy, ixz, iyz = values
    leading_minor = ixx * iyy - ixy * ixy
    determinant = (
        ixx * iyy * izz
        + 2.0 * ixy * ixz * iyz
        - ixx * iyz * iyz
        - iyy * ixz * ixz
        - izz * ixy * ixy
    )
    if leading_minor <= 0.0 or determinant <= 0.0:
        raise PhysicalConfigError(
            f"{path} must describe a positive-definite inertia matrix"
        )
    return values


def _validate_profile_id(profile_id: str) -> None:
    if (
        not isinstance(profile_id, str)
        or not _PROFILE_ID_PATTERN.fullmatch(profile_id)
    ):
        raise PhysicalConfigError(
            "profile_id must identify a versioned normal, low, asymmetric, "
            "or local-patch profile"
        )


def _validate_surface(
    profile_id: str,
    surface: SurfaceConfig,
) -> None:
    expected_surface = profile_id.rsplit("-v", 1)[0].replace("-", "_")
    if surface.profile != expected_surface:
        raise PhysicalConfigError(
            f"surface.profile {surface.profile!r} does not match "
            f"profile_id {profile_id!r}"
        )
    if surface.profile == "local_patch":
        if not surface.patch_enabled:
            raise PhysicalConfigError(
                "surface.patch_enabled must be true for local_patch"
            )
        if surface.patch_friction >= surface.base_floor_friction:
            raise PhysicalConfigError(
                "surface.patch_friction must be lower than "
                "surface.base_floor_friction for local_patch"
            )
    elif surface.patch_enabled:
        raise PhysicalConfigError(
            "surface.patch_enabled is only valid for local_patch"
        )
    if (
        surface.profile == "asymmetric"
        and math.isclose(
            surface.left_wheel_friction,
            surface.right_wheel_friction,
        )
    ):
        raise PhysicalConfigError(
            "asymmetric surface requires different left and right friction"
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value
