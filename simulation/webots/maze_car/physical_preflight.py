"""Physical vehicle-envelope checks for versioned rectangular maps."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from rdk_maze_tuner.core.maze_definition import MapDefinition


MAP_GEOMETRY_UNSAFE = "MAP_GEOMETRY_UNSAFE"


@dataclass(frozen=True)
class PhysicalPreflightReport:
    ok: bool
    code: str
    turning_envelope_mm: float
    minimum_required_passage_mm: float
    actual_passage_x_mm: float
    actual_passage_y_mm: float
    map_version_id: str
    map_digest: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PhysicalPreflightError(RuntimeError):
    def __init__(self, report: PhysicalPreflightReport) -> None:
        self.code = report.code
        self.report = report
        super().__init__(
            f"{report.code}: passage "
            f"{report.actual_passage_x_mm:.1f} x "
            f"{report.actual_passage_y_mm:.1f} mm is below "
            f"{report.minimum_required_passage_mm:.1f} mm"
        )


class PhysicalGeometryPreflight:
    """Check the rotating chassis envelope against the net cell passage."""

    def __init__(
        self,
        *,
        chassis_length_mm: float,
        chassis_width_mm: float,
        minimum_required_passage_mm: float = 320.0,
    ) -> None:
        self.chassis_length_mm = _positive_finite(
            chassis_length_mm,
            "chassis_length_mm",
        )
        self.chassis_width_mm = _positive_finite(
            chassis_width_mm,
            "chassis_width_mm",
        )
        self.minimum_required_passage_mm = _positive_finite(
            minimum_required_passage_mm,
            "minimum_required_passage_mm",
        )

    @property
    def turning_envelope_mm(self) -> float:
        return math.hypot(
            self.chassis_length_mm,
            self.chassis_width_mm,
        )

    def check(
        self,
        definition: MapDefinition,
        *,
        map_version_id: str | None,
    ) -> PhysicalPreflightReport:
        actual_x = (
            float(definition.cell_width_mm)
            - float(definition.wall_thickness_mm)
        )
        actual_y = (
            float(definition.cell_height_mm)
            - float(definition.wall_thickness_mm)
        )
        safe = (
            actual_x >= self.minimum_required_passage_mm
            and actual_y >= self.minimum_required_passage_mm
        )
        return PhysicalPreflightReport(
            ok=safe,
            code="OK" if safe else MAP_GEOMETRY_UNSAFE,
            turning_envelope_mm=self.turning_envelope_mm,
            minimum_required_passage_mm=(
                self.minimum_required_passage_mm
            ),
            actual_passage_x_mm=actual_x,
            actual_passage_y_mm=actual_y,
            map_version_id=str(map_version_id or "unversioned"),
            map_digest=definition.content_digest,
        )

    def require_safe(
        self,
        definition: MapDefinition,
        *,
        map_version_id: str | None,
    ) -> PhysicalPreflightReport:
        report = self.check(
            definition,
            map_version_id=map_version_id,
        )
        if not report.ok:
            raise PhysicalPreflightError(report)
        return report


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive and finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be positive and finite"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number
