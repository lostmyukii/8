import math

import pytest

from rdk_maze_tuner.core.maze_validation import validate_map_definition
from simulation.webots.maze_car.map_loader import compile_map
from simulation.webots.maze_car.physical_preflight import (
    MAP_GEOMETRY_UNSAFE,
    PhysicalGeometryPreflight,
    PhysicalPreflightError,
)


def map_definition(*, width_mm=450, height_mm=450, wall_mm=40):
    return validate_map_definition(
        {
            "rows": 2,
            "cols": 2,
            "cell_width_mm": width_mm,
            "cell_height_mm": height_mm,
            "wall_thickness_mm": wall_mm,
            "wall_height_mm": 180,
            "start": {"x": 0, "y": 1, "heading": "N"},
            "goals": [{"x": 1, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
                {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
                {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
                {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )


def preflight():
    return PhysicalGeometryPreflight(
        chassis_length_mm=230.0,
        chassis_width_mm=160.0,
        minimum_required_passage_mm=320.0,
    )


def test_vehicle_turning_envelope_and_safe_passage_report():
    definition = map_definition()

    report = preflight().check(
        definition,
        map_version_id="map-safe-v1",
    )

    assert report.ok is True
    assert report.code == "OK"
    assert report.turning_envelope_mm == pytest.approx(
        math.hypot(230, 160)
    )
    assert report.minimum_required_passage_mm == 320.0
    assert report.actual_passage_x_mm == 410.0
    assert report.actual_passage_y_mm == 410.0
    assert report.map_version_id == "map-safe-v1"
    assert report.map_digest == definition.content_digest


def test_wall_thickness_is_subtracted_from_both_passage_dimensions():
    report = preflight().check(
        map_definition(width_mm=350, height_mm=340, wall_mm=40),
        map_version_id="map-tight-v1",
    )

    assert report.actual_passage_x_mm == 310.0
    assert report.actual_passage_y_mm == 300.0
    assert report.ok is False
    assert report.code == MAP_GEOMETRY_UNSAFE


def test_unsafe_map_can_still_compile_but_physical_preflight_blocks_start():
    definition = map_definition(
        width_mm=300,
        height_mm=300,
        wall_mm=20,
    )

    compiled = compile_map(definition)
    assert compiled.cell_width_m == 0.3
    assert compiled.cell_height_m == 0.3

    with pytest.raises(PhysicalPreflightError) as raised:
        preflight().require_safe(
            definition,
            map_version_id="map-deterministic-only",
        )

    assert raised.value.code == MAP_GEOMETRY_UNSAFE
    assert raised.value.report.actual_passage_x_mm == 280.0
    assert raised.value.report.actual_passage_y_mm == 280.0


@pytest.mark.parametrize(
    ("length", "width", "required"),
    [
        (0, 160, 320),
        (230, -1, 320),
        (230, 160, 0),
        (float("nan"), 160, 320),
    ],
)
def test_non_positive_or_non_finite_preflight_configuration_is_rejected(
    length,
    width,
    required,
):
    with pytest.raises(ValueError):
        PhysicalGeometryPreflight(
            chassis_length_mm=length,
            chassis_width_mm=width,
            minimum_required_passage_mm=required,
        )
