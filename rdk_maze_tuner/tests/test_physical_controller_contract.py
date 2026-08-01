import inspect
from pathlib import Path

from simulation.webots.maze_car.controllers.maze_physical_controller.physical_engine import (
    PhysicalMazeEngine,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_world import (
    PhysicalWorldConfigurator,
)
from simulation.webots.maze_car.map_loader import (
    compile_map,
    default_map_definition,
)


ROOT = Path(
    "simulation/webots/maze_car/controllers/maze_physical_controller"
)


class FakeField:
    def __init__(self) -> None:
        self.value = None
        self.values = []

    def setSFVec3f(self, value):
        self.value = tuple(value)

    def setSFRotation(self, value):
        self.value = tuple(value)

    def setSFFloat(self, value):
        self.value = value

    def setSFString(self, value):
        self.value = value

    def setMFVec3f(self, index, value):
        while len(self.values) <= index:
            self.values.append(None)
        self.values[index] = tuple(value)


class FakeRobotNode:
    def __init__(self) -> None:
        self.fields = {
            name: FakeField()
            for name in (
                "translation",
                "rotation",
                "bodyMass",
                "wheelMass",
                "maxWheelVelocity",
                "maxWheelTorque",
                "centerOfMass",
                "inertiaMatrix",
                "leftContactMaterial",
                "rightContactMaterial",
                "tofLookupTable",
            )
        }
        self.reset_count = 0
        self.saved_states = []
        self.loaded_states = []
        self.joints = {
            name: FakeJoint()
            for name in (
                "LEFT_WHEEL_JOINT",
                "RIGHT_WHEEL_JOINT",
                "CASTER_JOINT",
            )
        }

    def getField(self, name):
        return self.fields.get(name)

    def resetPhysics(self):
        self.reset_count += 1

    def saveState(self, name):
        self.saved_states.append(name)

    def loadState(self, name):
        self.loaded_states.append(name)

    def getFromProtoDef(self, name):
        return self.joints.get(name)


class FakeJoint:
    def __init__(self) -> None:
        self.positions = []

    def setJointPosition(self, position, index=1):
        self.positions.append((position, index))


class FakeSupervisor:
    def __init__(self, robot) -> None:
        self.robot = robot
        self.steps = []
        self.physics_reset_count = 0

    def getSelf(self):
        return self.robot

    def getFromDef(self, _name):
        return None

    def step(self, period):
        self.steps.append(period)
        return 0

    def simulationResetPhysics(self):
        self.physics_reset_count += 1


class FakeMapLoader:
    def load(self, definition):
        return compile_map(definition)


def test_only_world_reset_pose_can_teleport_or_reset_physics():
    restricted = ("setSFVec3f", "setSFRotation", "resetPhysics")
    reset_source = inspect.getsource(PhysicalWorldConfigurator.reset_pose)
    assert all(token in reset_source for token in restricted)

    for path in ROOT.glob("*.py"):
        if path.name == "physical_world.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in restricted:
            assert token not in text, f"{token} leaked into {path}"


def test_physical_engine_tick_has_fixed_read_control_write_telemetry_order():
    source = inspect.getsource(PhysicalMazeEngine.tick)

    assert source.index(".sample(") < source.index("._controller.tick(")
    assert source.index("._controller.tick(") < source.index(
        ".command_wheels("
    )
    assert source.index(".command_wheels(") < source.index(
        "._build_telemetry("
    )
    assert "translation" not in source
    assert "rotation" not in source


def test_world_configurator_applies_profile_and_resets_to_map_start():
    robot = FakeRobotNode()
    supervisor = FakeSupervisor(robot)
    world = PhysicalWorldConfigurator(
        supervisor,
        map_loader=FakeMapLoader(),
        settle_steps=3,
        basic_time_step_ms=8,
    )
    definition = default_map_definition()
    compiled = world.load_map(definition)

    from simulation.webots.maze_car.physical_config import (
        PhysicalProfileRepository,
    )

    profile = PhysicalProfileRepository().get("normal-v1")
    world.configure_sensor_mode(ideal=True)
    assert world.apply_profile(profile) is True
    assert world.apply_profile(profile) is False
    world.reset_pose(compiled)

    assert robot.fields["bodyMass"].value == 1.08
    assert robot.fields["wheelMass"].value == 0.06
    assert robot.fields["maxWheelVelocity"].value == 20.0
    assert robot.fields["maxWheelTorque"].value == 0.60
    assert robot.fields["centerOfMass"].values[0] == (0.0, 0.07, 0.01)
    assert robot.fields["tofLookupTable"].values == [
        (0.03, 0.03, 0.0),
        (2.0, 2.0, 0.0),
    ]
    assert robot.fields["translation"].value == (-0.9, 0.0, 0.9)
    assert robot.fields["rotation"].value == (0.0, 1.0, 0.0, 0.0)
    assert robot.reset_count == 1
    assert supervisor.physics_reset_count == 1
    assert robot.joints["LEFT_WHEEL_JOINT"].positions == [(0.0, 1)]
    assert robot.joints["RIGHT_WHEEL_JOINT"].positions == [(0.0, 1)]
    assert robot.joints["CASTER_JOINT"].positions == [
        (0.0, 1),
        (0.0, 2),
        (0.0, 3),
    ]
    assert supervisor.steps == [8, 8, 8]

    world.refresh_device_samples()

    assert supervisor.steps == [8, 8, 8, 8, 8]

    world.reset_pose(compiled)

    assert robot.reset_count == 2
    assert supervisor.physics_reset_count == 2
    assert supervisor.steps == [8, 8, 8, 8, 8, 8, 8, 8]
