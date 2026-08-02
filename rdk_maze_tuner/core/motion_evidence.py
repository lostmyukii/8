"""Immutable arrival thresholds and pure motion-evidence decisions."""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite
from numbers import Real
from typing import Any, Mapping


MOTION_EVIDENCE_UNSAFE = "MOTION_EVIDENCE_UNSAFE"
MOTION_RECOVERY_FAILED = "MOTION_RECOVERY_FAILED"
POSE_UNCERTAIN = "POSE_UNCERTAIN"
WHEEL_SLIP_DETECTED = "WHEEL_SLIP_DETECTED"


@dataclass(frozen=True)
class ArrivalVerificationConfig:
    nominal_position_error_ratio: float = 0.10
    recoverable_position_error_ratio: float = 0.20
    nominal_heading_error_deg: float = 8.0
    recoverable_heading_error_deg: float = 12.0
    goal_min_confidence: float = 0.80
    max_recovery_attempts_per_cell: int = 2

    def __post_init__(self) -> None:
        numeric_names = (
            "nominal_position_error_ratio",
            "recoverable_position_error_ratio",
            "nominal_heading_error_deg",
            "recoverable_heading_error_deg",
            "goal_min_confidence",
        )
        for name in numeric_names:
            if not _finite_number(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if not 0 < self.nominal_position_error_ratio <= 0.5:
            raise ValueError(
                "nominal_position_error_ratio must be in (0, 0.5]"
            )
        if not (
            self.nominal_position_error_ratio
            <= self.recoverable_position_error_ratio
            <= 0.5
        ):
            raise ValueError(
                "nominal position error must not exceed recoverable position "
                "error"
            )
        if not 0 < self.nominal_heading_error_deg <= 45:
            raise ValueError(
                "nominal_heading_error_deg must be in (0, 45]"
            )
        if not (
            self.nominal_heading_error_deg
            <= self.recoverable_heading_error_deg
            <= 45
        ):
            raise ValueError(
                "nominal heading error must not exceed recoverable heading "
                "error"
            )
        if not 0 <= self.goal_min_confidence <= 1:
            raise ValueError("goal_min_confidence must be in [0, 1]")
        if (
            not isinstance(self.max_recovery_attempts_per_cell, int)
            or isinstance(self.max_recovery_attempts_per_cell, bool)
            or not 1 <= self.max_recovery_attempts_per_cell <= 10
        ):
            raise ValueError(
                "max_recovery_attempts_per_cell must be an integer in [1, 10]"
            )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "ArrivalVerificationConfig":
        if not isinstance(value, Mapping):
            raise ValueError("arrival verification config must be a mapping")
        names = {field.name for field in fields(cls)}
        unknown = set(value) - names
        missing = names - set(value)
        if unknown:
            raise ValueError(
                "unknown arrival verification fields: "
                + ", ".join(sorted(unknown))
            )
        if missing:
            raise ValueError(
                "missing arrival verification fields: "
                + ", ".join(sorted(missing))
            )
        return cls(**{name: value[name] for name in names})

    def to_dict(self) -> dict[str, float | int]:
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
        }


@dataclass(frozen=True)
class MotionEvidenceInput:
    action_id: str
    action_name: str
    expected_distance_mm: float
    measured_distance_mm: float
    encoder_displacement_mm: float
    external_displacement_mm: float
    expected_heading_deg: float
    measured_heading_deg: float
    pose_confidence: float
    recovery_attempts: int = 0
    correction_evidence_available: bool = False
    external_evidence_available: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MotionEvidenceInput":
        if not isinstance(value, Mapping):
            raise ValueError("motion evidence must be a mapping")
        names = {field.name for field in fields(cls)}
        unknown = set(value) - names - {"sim_truth"}
        if unknown:
            raise ValueError(
                "unknown motion evidence fields: "
                + ", ".join(sorted(unknown))
            )
        return cls(**{name: value[name] for name in names if name in value})


@dataclass(frozen=True)
class RecoverySuggestion:
    kind: str
    remaining_distance_mm: float | None = None
    heading_delta_deg: float | None = None
    max_distance_mm: float | None = None
    max_heading_deg: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "remaining_distance_mm": self.remaining_distance_mm,
            "heading_delta_deg": self.heading_delta_deg,
            "max_distance_mm": self.max_distance_mm,
            "max_heading_deg": self.max_heading_deg,
        }


@dataclass(frozen=True)
class MotionEvidenceDecision:
    status: str
    code: str | None
    position_error_ratio: float
    heading_error_deg: float
    pose_confidence: float
    recovery: RecoverySuggestion | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "position_error_ratio": self.position_error_ratio,
            "heading_error_deg": self.heading_error_deg,
            "pose_confidence": self.pose_confidence,
            "recovery": (
                None if self.recovery is None else self.recovery.to_dict()
            ),
            "reasons": list(self.reasons),
        }


class MotionEvidenceGate:
    """Evaluate algorithm evidence without access to simulation truth."""

    def __init__(
        self,
        config: ArrivalVerificationConfig | None = None,
    ) -> None:
        self.config = config or ArrivalVerificationConfig()

    def evaluate(
        self,
        evidence: MotionEvidenceInput,
    ) -> MotionEvidenceDecision:
        self._validate_input(evidence)
        position_error_ratio = (
            abs(
                evidence.expected_distance_mm
                - evidence.measured_distance_mm
            )
            / evidence.expected_distance_mm
            if evidence.expected_distance_mm > 0
            else 0.0
        )
        heading_error = _heading_error_deg(
            evidence.expected_heading_deg,
            evidence.measured_heading_deg,
        )
        metrics = {
            "position_error_ratio": position_error_ratio,
            "heading_error_deg": heading_error,
            "pose_confidence": evidence.pose_confidence,
        }

        if evidence.pose_confidence < self.config.goal_min_confidence:
            return _unsafe(
                POSE_UNCERTAIN,
                "pose confidence is below the frozen threshold",
                **metrics,
            )
        if self._wheel_slip_detected(evidence):
            return _unsafe(
                WHEEL_SLIP_DETECTED,
                "encoders moved but external pose evidence did not",
                **metrics,
            )

        nominal = (
            position_error_ratio
            <= self.config.nominal_position_error_ratio
            and heading_error
            <= self.config.nominal_heading_error_deg
        )
        if nominal:
            return MotionEvidenceDecision(
                status="accepted",
                code=None,
                reasons=(),
                recovery=None,
                **metrics,
            )

        recoverable = (
            position_error_ratio
            <= self.config.recoverable_position_error_ratio
            and heading_error
            <= self.config.recoverable_heading_error_deg
        )
        if recoverable and not evidence.correction_evidence_available:
            return _unsafe(
                POSE_UNCERTAIN,
                "recoverable error lacks wall or IMU correction evidence",
                **metrics,
            )
        if recoverable and (
            evidence.recovery_attempts
            >= self.config.max_recovery_attempts_per_cell
        ):
            return _unsafe(
                MOTION_RECOVERY_FAILED,
                "per-cell recovery limit has been reached",
                **metrics,
            )
        if recoverable:
            return MotionEvidenceDecision(
                status="recoverable",
                code=None,
                recovery=_recovery_for(
                    evidence,
                    position_error_ratio=position_error_ratio,
                    heading_error_deg=heading_error,
                    config=self.config,
                ),
                reasons=("motion evidence requires bounded correction",),
                **metrics,
            )
        return _unsafe(
            MOTION_EVIDENCE_UNSAFE,
            "position or heading error exceeds the recoverable threshold",
            **metrics,
        )

    @staticmethod
    def _validate_input(evidence: MotionEvidenceInput) -> None:
        for name in (
            "expected_distance_mm",
            "measured_distance_mm",
            "encoder_displacement_mm",
            "external_displacement_mm",
            "expected_heading_deg",
            "measured_heading_deg",
            "pose_confidence",
        ):
            if not _finite_number(getattr(evidence, name)):
                raise ValueError(f"{name} must be finite")
        if evidence.expected_distance_mm < 0:
            raise ValueError("expected_distance_mm must not be negative")
        if evidence.measured_distance_mm < 0:
            raise ValueError("measured_distance_mm must not be negative")
        if not 0 <= evidence.pose_confidence <= 1:
            raise ValueError("pose_confidence must be in [0, 1]")
        if (
            not isinstance(evidence.recovery_attempts, int)
            or isinstance(evidence.recovery_attempts, bool)
            or evidence.recovery_attempts < 0
        ):
            raise ValueError("recovery_attempts must be a non-negative integer")
        if not isinstance(evidence.external_evidence_available, bool):
            raise ValueError(
                "external_evidence_available must be boolean"
            )

    @staticmethod
    def _wheel_slip_detected(evidence: MotionEvidenceInput) -> bool:
        if evidence.action_name != "move_cell":
            return False
        if not evidence.external_evidence_available:
            return False
        expected = evidence.expected_distance_mm
        if expected <= 0:
            return False
        return (
            evidence.encoder_displacement_mm >= expected * 0.5
            and evidence.external_displacement_mm <= expected * 0.05
        )


def _recovery_for(
    evidence: MotionEvidenceInput,
    *,
    position_error_ratio: float,
    heading_error_deg: float,
    config: ArrivalVerificationConfig,
) -> RecoverySuggestion:
    if position_error_ratio > config.nominal_position_error_ratio:
        remaining = max(
            0.0,
            evidence.expected_distance_mm - evidence.measured_distance_mm,
        )
        return RecoverySuggestion(
            kind="nudge_forward",
            remaining_distance_mm=remaining,
            max_distance_mm=evidence.expected_distance_mm * 0.25,
        )
    delta = _signed_heading_delta_deg(
        evidence.expected_heading_deg,
        evidence.measured_heading_deg,
    )
    return RecoverySuggestion(
        kind="align_heading",
        heading_delta_deg=delta,
        max_heading_deg=15.0,
    )


def _unsafe(
    code: str,
    reason: str,
    *,
    position_error_ratio: float,
    heading_error_deg: float,
    pose_confidence: float,
) -> MotionEvidenceDecision:
    return MotionEvidenceDecision(
        status="unsafe",
        code=code,
        position_error_ratio=position_error_ratio,
        heading_error_deg=heading_error_deg,
        pose_confidence=pose_confidence,
        reasons=(reason,),
    )


def _heading_error_deg(expected: float, measured: float) -> float:
    return abs(_signed_heading_delta_deg(expected, measured))


def _signed_heading_delta_deg(expected: float, measured: float) -> float:
    return (expected - measured + 180.0) % 360.0 - 180.0


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and isfinite(float(value))
    )
