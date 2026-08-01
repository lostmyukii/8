"""Immutable P3/P4 scenario contracts and structured run metrics."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from simulation.webots.maze_car.physical_config import (
    PhysicalConfigError,
    PhysicalProfile,
    PhysicalProfileRepository,
)


DEFAULT_SCENARIO_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "acceptance_scenarios.yaml"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_WORLDS = frozenset(
    {
        "maze_physical_calibration.wbt",
        "maze_physical_world.wbt",
    }
)
_SCENARIO_FIELDS = frozenset(
    {
        "scenario_id",
        "physical_profile_id",
        "physical_profile_digest",
        "world",
        "map_version_id",
        "map_digest",
        "seed",
        "actions",
        "expected_observations",
        "acceptance_thresholds",
        "timeout_ms",
    }
)
_ACTION_FIELDS = frozenset(
    {"name", "target_ticks", "speed", "repeat"}
)
_THRESHOLD_FIELDS = frozenset(
    {
        "max_distance_error_mm",
        "max_heading_error_deg",
        "max_turn_error_deg",
        "min_success_rate",
        "min_mean_abs_slip",
        "min_trajectory_difference_mm",
        "min_slip_difference",
        "min_yaw_difference_deg",
        "min_patch_slip_increase",
        "min_surface_transitions",
    }
)
_ACTION_NAMES = frozenset(
    {"move_cell", "turn_left", "turn_right", "turn_back"}
)


class PhysicalScenarioError(ValueError):
    """Raised when scenario content is mutable, unsafe, or ambiguous."""


@dataclass(frozen=True)
class ScenarioAction:
    name: str
    target_ticks: int
    speed: float
    repeat: int


@dataclass(frozen=True)
class PhysicalScenario:
    scenario_id: str
    physical_profile_id: str
    physical_profile_digest: str
    world: str
    map_version_id: str
    map_digest: str
    seed: int
    actions: tuple[ScenarioAction, ...]
    expected_observations: tuple[str, ...]
    acceptance_thresholds: tuple[tuple[str, float], ...]
    timeout_ms: int

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "physical_profile_id": self.physical_profile_id,
            "physical_profile_digest": self.physical_profile_digest,
            "world": self.world,
            "map_version_id": self.map_version_id,
            "map_digest": self.map_digest,
            "seed": self.seed,
            "actions": [asdict(action) for action in self.actions],
            "expected_observations": list(
                self.expected_observations
            ),
            "acceptance_thresholds": dict(
                self.acceptance_thresholds
            ),
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True)
class ScenarioRunMetrics:
    truth_distance_mm: float
    encoder_distance_mm: float
    encoder_truth_gap_mm: float
    final_yaw_deg: float
    mean_abs_left_slip: float
    mean_abs_right_slip: float
    mean_abs_slip_difference: float
    surface_transitions: int
    collision_count: int
    completed_actions: int
    requested_actions: int
    success_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PhysicalScenarioRepository:
    def __init__(
        self,
        path: Path | str = DEFAULT_SCENARIO_PATH,
    ) -> None:
        try:
            payload = yaml.safe_load(
                Path(path).read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            raise PhysicalScenarioError(
                f"cannot load physical scenarios: {exc}"
            ) from exc
        self._load_payload(payload)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "PhysicalScenarioRepository":
        repository = cls.__new__(cls)
        repository._load_payload(payload)
        return repository

    def _load_payload(self, payload: object) -> None:
        root = _mapping(payload, "scenario document")
        _exact_fields(
            root,
            frozenset({"schema_version", "scenarios"}),
            "scenario document",
        )
        if root["schema_version"] != 1:
            raise PhysicalScenarioError(
                "scenario schema_version must be 1"
            )
        raw_scenarios = root["scenarios"]
        if (
            not isinstance(raw_scenarios, Sequence)
            or isinstance(raw_scenarios, (str, bytes))
            or not raw_scenarios
        ):
            raise PhysicalScenarioError(
                "scenarios must be a non-empty list"
            )
        profiles = PhysicalProfileRepository()
        scenarios: dict[str, PhysicalScenario] = {}
        for raw in raw_scenarios:
            scenario = _parse_scenario(raw, profiles=profiles)
            if scenario.scenario_id in scenarios:
                raise PhysicalScenarioError(
                    f"duplicate scenario_id: {scenario.scenario_id}"
                )
            scenarios[scenario.scenario_id] = scenario
        self.schema_version = 1
        self._scenarios = scenarios

    def list_scenarios(self) -> tuple[PhysicalScenario, ...]:
        return tuple(
            self._scenarios[key] for key in sorted(self._scenarios)
        )

    def get(self, scenario_id: str) -> PhysicalScenario:
        try:
            return self._scenarios[str(scenario_id)]
        except KeyError as exc:
            raise PhysicalScenarioError(
                f"unknown scenario_id: {scenario_id}"
            ) from exc

    def resolve_profile(
        self,
        scenario_id: str,
        *,
        profiles: PhysicalProfileRepository,
        requested_profile_id: str | None = None,
    ) -> PhysicalProfile:
        scenario = self.get(scenario_id)
        if (
            requested_profile_id is not None
            and str(requested_profile_id)
            != scenario.physical_profile_id
        ):
            raise PhysicalScenarioError(
                "scenario is bound to its immutable physical profile"
            )
        profile = profiles.get(
            scenario.physical_profile_id,
            expected_digest=scenario.physical_profile_digest,
        )
        if profile.random_seed != scenario.seed:
            raise PhysicalScenarioError(
                "scenario seed does not match its physical profile"
            )
        return profile

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenarios": [
                scenario.to_dict()
                for scenario in self.list_scenarios()
            ],
        }


def evaluate_scenario_frames(
    frames: Sequence[Mapping[str, Any]],
    *,
    ticks_per_mm: float,
    completed_actions: int,
    requested_actions: int,
) -> ScenarioRunMetrics:
    if len(frames) < 2:
        raise PhysicalScenarioError(
            "scenario metrics require at least two telemetry frames"
        )
    ticks_scale = _positive_finite(
        ticks_per_mm,
        "ticks_per_mm",
    )
    truths = []
    left_slip = []
    right_slip = []
    slip_difference = []
    surfaces = []
    collisions = []
    for frame in frames:
        truth = _mapping(frame.get("sim_truth"), "sim_truth")
        x = _finite(truth.get("x_mm"), "sim_truth.x_mm")
        y = _finite(truth.get("y_mm"), "sim_truth.y_mm")
        yaw = _finite(truth.get("yaw_deg"), "sim_truth.yaw_deg")
        left = _finite(
            truth.get("left_slip_rate"),
            "sim_truth.left_slip_rate",
        )
        right = _finite(
            truth.get("right_slip_rate"),
            "sim_truth.right_slip_rate",
        )
        surface = str(truth.get("active_surface") or "").strip()
        if not surface:
            raise PhysicalScenarioError(
                "sim_truth.active_surface is required"
            )
        collision = truth.get("collision_count", 0)
        if (
            isinstance(collision, bool)
            or not isinstance(collision, int)
            or collision < 0
        ):
            raise PhysicalScenarioError(
                "sim_truth.collision_count is invalid"
            )
        truths.append((x, y, yaw))
        wheel_speed_values = (
            frame.get("wheel_speed_left_rad_s"),
            frame.get("wheel_speed_right_rad_s"),
        )
        moving = all(value is None for value in wheel_speed_values)
        if not moving:
            left_wheel_speed = _finite(
                wheel_speed_values[0] or 0.0,
                "wheel_speed_left_rad_s",
            )
            right_wheel_speed = _finite(
                wheel_speed_values[1] or 0.0,
                "wheel_speed_right_rad_s",
            )
            moving = (
                abs(left_wheel_speed) + abs(right_wheel_speed)
            ) >= 1.0
        if moving:
            left_slip.append(abs(left))
            right_slip.append(abs(right))
            slip_difference.append(abs(left - right))
        surfaces.append(surface)
        collisions.append(collision)

    first_frame = frames[0]
    last_frame = frames[-1]
    enc_left = abs(
        _integer(last_frame.get("enc_left"), "enc_left")
        - _integer(first_frame.get("enc_left"), "enc_left")
    )
    enc_right = abs(
        _integer(last_frame.get("enc_right"), "enc_right")
        - _integer(first_frame.get("enc_right"), "enc_right")
    )
    truth_distance = math.hypot(
        truths[-1][0] - truths[0][0],
        truths[-1][1] - truths[0][1],
    )
    encoder_distance = (
        (enc_left + enc_right) / 2.0 / ticks_scale
    )
    requested = _positive_integer(
        requested_actions,
        "requested_actions",
    )
    completed = _non_negative_integer(
        completed_actions,
        "completed_actions",
    )
    return ScenarioRunMetrics(
        truth_distance_mm=truth_distance,
        encoder_distance_mm=encoder_distance,
        encoder_truth_gap_mm=abs(
            encoder_distance - truth_distance
        ),
        final_yaw_deg=truths[-1][2] % 360.0,
        mean_abs_left_slip=(
            sum(left_slip) / len(left_slip) if left_slip else 0.0
        ),
        mean_abs_right_slip=(
            sum(right_slip) / len(right_slip)
            if right_slip
            else 0.0
        ),
        mean_abs_slip_difference=(
            sum(slip_difference) / len(slip_difference)
            if slip_difference
            else 0.0
        ),
        surface_transitions=sum(
            before != after
            for before, after in zip(surfaces, surfaces[1:])
        ),
        collision_count=max(collisions),
        completed_actions=completed,
        requested_actions=requested,
        success_rate=min(1.0, completed / requested),
    )


def _parse_scenario(
    raw: object,
    *,
    profiles: PhysicalProfileRepository,
) -> PhysicalScenario:
    value = _mapping(raw, "scenario")
    _exact_fields(value, _SCENARIO_FIELDS, "scenario")
    scenario_id = _non_empty_text(
        value["scenario_id"],
        "scenario_id",
    )
    profile_id = _non_empty_text(
        value["physical_profile_id"],
        "physical_profile_id",
    )
    profile_digest = _digest(
        value["physical_profile_digest"],
        "physical_profile_digest",
    )
    try:
        profile = profiles.get(
            profile_id,
            expected_digest=profile_digest,
        )
    except PhysicalConfigError as exc:
        raise PhysicalScenarioError(str(exc)) from exc
    seed = _non_negative_integer(value["seed"], "seed")
    if seed != profile.random_seed:
        raise PhysicalScenarioError(
            f"{scenario_id} seed does not match profile"
        )
    world = _non_empty_text(value["world"], "world")
    if world not in _ALLOWED_WORLDS:
        raise PhysicalScenarioError(
            f"unsupported physical world: {world}"
        )
    raw_actions = value["actions"]
    if (
        not isinstance(raw_actions, Sequence)
        or isinstance(raw_actions, (str, bytes))
        or not raw_actions
    ):
        raise PhysicalScenarioError("actions must be non-empty")
    actions = tuple(_parse_action(item) for item in raw_actions)
    raw_observations = value["expected_observations"]
    if (
        not isinstance(raw_observations, Sequence)
        or isinstance(raw_observations, (str, bytes))
        or not raw_observations
    ):
        raise PhysicalScenarioError(
            "expected_observations must be non-empty"
        )
    observations = tuple(
        _non_empty_text(item, "expected_observation")
        for item in raw_observations
    )
    threshold_payload = _mapping(
        value["acceptance_thresholds"],
        "acceptance_thresholds",
    )
    unknown_thresholds = (
        set(threshold_payload) - _THRESHOLD_FIELDS
    )
    if unknown_thresholds or not threshold_payload:
        raise PhysicalScenarioError(
            "acceptance_thresholds contains unknown or no fields"
        )
    thresholds = tuple(
        sorted(
            (
                str(name),
                _positive_finite(
                    threshold,
                    f"acceptance_thresholds.{name}",
                ),
            )
            for name, threshold in threshold_payload.items()
        )
    )
    threshold_dict = dict(thresholds)
    if threshold_dict.get("min_success_rate", 1.0) > 1.0:
        raise PhysicalScenarioError(
            "min_success_rate must not exceed 1"
        )
    return PhysicalScenario(
        scenario_id=scenario_id,
        physical_profile_id=profile_id,
        physical_profile_digest=profile_digest,
        world=world,
        map_version_id=_non_empty_text(
            value["map_version_id"],
            "map_version_id",
        ),
        map_digest=_digest(value["map_digest"], "map_digest"),
        seed=seed,
        actions=actions,
        expected_observations=observations,
        acceptance_thresholds=thresholds,
        timeout_ms=_positive_integer(
            value["timeout_ms"],
            "timeout_ms",
        ),
    )


def _parse_action(raw: object) -> ScenarioAction:
    value = _mapping(raw, "scenario action")
    _exact_fields(value, _ACTION_FIELDS, "scenario action")
    name = _non_empty_text(value["name"], "action.name")
    if name not in _ACTION_NAMES:
        raise PhysicalScenarioError(
            f"unsupported scenario action: {name}"
        )
    speed = _positive_finite(value["speed"], "action.speed")
    if speed > 1.0:
        raise PhysicalScenarioError(
            "action.speed must not exceed 1"
        )
    return ScenarioAction(
        name=name,
        target_ticks=_positive_integer(
            value["target_ticks"],
            "action.target_ticks",
        ),
        speed=speed,
        repeat=_positive_integer(
            value["repeat"],
            "action.repeat",
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PhysicalScenarioError(f"{name} must be an object")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    name: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise PhysicalScenarioError(
            f"{name} fields mismatch: "
            f"missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PhysicalScenarioError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object, name: str) -> str:
    text = _non_empty_text(value, name)
    if _DIGEST.fullmatch(text) is None:
        raise PhysicalScenarioError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return text


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise PhysicalScenarioError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicalScenarioError(
            f"{name} must be finite"
        ) from exc
    if not math.isfinite(number):
        raise PhysicalScenarioError(f"{name} must be finite")
    return number


def _positive_finite(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise PhysicalScenarioError(f"{name} must be positive")
    return number


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhysicalScenarioError(f"{name} must be an integer")
    return int(value)


def _positive_integer(value: object, name: str) -> int:
    number = _integer(value, name)
    if number <= 0:
        raise PhysicalScenarioError(f"{name} must be positive")
    return number


def _non_negative_integer(value: object, name: str) -> int:
    number = _integer(value, name)
    if number < 0:
        raise PhysicalScenarioError(
            f"{name} must not be negative"
        )
    return number
