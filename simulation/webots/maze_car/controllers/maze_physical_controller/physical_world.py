"""Supervisor-owned physical profile, map, and reset-boundary operations."""

from __future__ import annotations

import math
from typing import Any

from rdk_maze_tuner.core.maze_definition import MapDefinition
from simulation.webots.maze_car.map_loader import (
    CompiledMap,
    WebotsMapLoader,
)
from simulation.webots.maze_car.physical_config import PhysicalProfile


_HEADING_ROTATIONS = {
    "N": 0.0,
    "E": -math.pi / 2.0,
    "S": math.pi,
    "W": math.pi / 2.0,
}


class PhysicalWorldConfigurator:
    """Apply structural changes only while the engine is at reset."""

    def __init__(
        self,
        supervisor: Any,
        *,
        map_loader: Any | None = None,
        robot_node: Any | None = None,
        settle_steps: int = 5,
        basic_time_step_ms: int = 8,
    ) -> None:
        self._supervisor = supervisor
        self._robot = robot_node or supervisor.getSelf()
        if self._robot is None:
            raise RuntimeError("physical robot Supervisor node is missing")
        self._map_loader = map_loader or WebotsMapLoader(supervisor)
        self._settle_steps = max(0, int(settle_steps))
        self._basic_time_step_ms = int(basic_time_step_ms)
        self._compiled: CompiledMap | None = None
        self._applied_profile_digest: str | None = None

    def load_map(self, definition: MapDefinition) -> CompiledMap:
        compiled = self._map_loader.load(definition)
        if compiled.cell_width_m <= 0 or compiled.cell_height_m <= 0:
            raise RuntimeError("compiled map has invalid cell dimensions")
        self._compiled = compiled
        return compiled

    def apply_profile(self, profile: PhysicalProfile) -> bool:
        if self._applied_profile_digest == profile.digest:
            return False
        self._set_float("bodyMass", profile.body.body_mass_kg)
        self._set_float("wheelMass", profile.body.wheel_mass_kg)
        self._set_float(
            "maxWheelVelocity",
            profile.motor.max_velocity_rad_s,
        )
        self._set_float(
            "maxWheelTorque",
            profile.motor.max_torque_nm,
        )
        self._set_vector_item(
            "centerOfMass",
            0,
            profile.body.center_of_mass_m,
        )
        inertia = profile.body.inertia_matrix_kg_m2
        self._set_vector_item("inertiaMatrix", 0, inertia[:3])
        self._set_vector_item("inertiaMatrix", 1, inertia[3:])
        left_material, right_material = _wheel_materials(profile)
        self._set_text("leftContactMaterial", left_material)
        self._set_text("rightContactMaterial", right_material)
        self._applied_profile_digest = profile.digest
        return True

    def configure_sensor_mode(self, *, ideal: bool) -> None:
        # Keep Webots' native noise disabled.  The ESP32-like adapter owns the
        # seeded noise/dropout model so reset can reproduce the same sequence.
        native_noise = 0.0
        lookup_table = self._required_field("tofLookupTable")
        lookup_table.setMFVec3f(
            0,
            [0.03, 0.03, native_noise],
        )
        lookup_table.setMFVec3f(
            1,
            [2.0, 2.0, native_noise],
        )

    def reset_pose(self, compiled: CompiledMap | None = None) -> None:
        active_map = compiled or self._compiled
        if active_map is None:
            raise RuntimeError("cannot reset physical pose before loading a map")
        start_x, start_y = active_map.start_cell
        x = (
            start_x - (active_map.cols - 1) / 2.0
        ) * active_map.cell_width_m
        z = (
            start_y - (active_map.rows - 1) / 2.0
        ) * active_map.cell_height_m
        translation = self._required_field("translation")
        rotation = self._required_field("rotation")
        translation.setSFVec3f([x, 0.0, z])
        rotation.setSFRotation(
            [
                0.0,
                1.0,
                0.0,
                _HEADING_ROTATIONS[active_map.start_heading],
            ]
        )
        self._reset_joint_positions()
        self._robot.resetPhysics()
        reset_all_physics = getattr(
            self._supervisor,
            "simulationResetPhysics",
            None,
        )
        if reset_all_physics is not None:
            reset_all_physics()
        self._settle()

    def refresh_device_samples(self, *, steps: int = 2) -> None:
        """Advance newly enabled Webots sensors past their first sample."""

        for _ in range(max(1, int(steps))):
            if self._supervisor.step(self._basic_time_step_ms) == -1:
                break

    def _settle(self) -> None:
        for _ in range(self._settle_steps):
            if self._supervisor.step(self._basic_time_step_ms) == -1:
                break

    def _reset_joint_positions(self) -> None:
        get_from_proto = getattr(self._robot, "getFromProtoDef", None)
        if get_from_proto is None:
            return
        joint_specs = (
            ("LEFT_WHEEL_JOINT", (1,)),
            ("RIGHT_WHEEL_JOINT", (1,)),
            ("CASTER_JOINT", (1, 2, 3)),
        )
        for name, axes in joint_specs:
            joint = get_from_proto(name)
            if joint is None:
                raise RuntimeError(
                    f"physical robot joint is missing: {name}"
                )
            for axis in axes:
                joint.setJointPosition(0.0, axis)

    def _required_field(self, name: str) -> Any:
        field = self._robot.getField(name)
        if field is None:
            raise RuntimeError(f"physical robot field is missing: {name}")
        return field

    def _set_float(self, name: str, value: float) -> None:
        self._required_field(name).setSFFloat(float(value))

    def _set_text(self, name: str, value: str) -> None:
        self._required_field(name).setSFString(str(value))

    def _set_vector_item(
        self,
        name: str,
        index: int,
        value,
    ) -> None:
        self._required_field(name).setMFVec3f(
            int(index),
            [float(component) for component in value],
        )


def _wheel_materials(profile: PhysicalProfile) -> tuple[str, str]:
    surface = profile.surface
    left = (
        "maze_tire_left_low"
        if surface.left_wheel_friction < 0.5
        else "maze_tire_left"
    )
    right = (
        "maze_tire_right_low"
        if surface.right_wheel_friction < 0.5
        else "maze_tire_right"
    )
    return left, right
