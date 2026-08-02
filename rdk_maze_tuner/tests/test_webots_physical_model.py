import re
from pathlib import Path


ROOT = Path("simulation/webots/maze_car")
PROTO = ROOT / "protos/PhysicalMazeCar.proto"
CALIBRATION_WORLD = ROOT / "worlds/maze_physical_calibration.wbt"
MAZE_WORLD = ROOT / "worlds/maze_physical_world.wbt"
CONTROLLER = (
    ROOT
    / "controllers"
    / "maze_physical_controller"
    / "maze_physical_controller.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_physical_proto_has_confirmed_geometry_and_mass_fields():
    text = _read(PROTO)

    assert text.startswith("#VRML_SIM R2025a utf8")
    assert "PROTO PhysicalMazeCar [" in text
    assert "field SFFloat wheelRadius 0.0325" in text
    assert "field SFFloat wheelWidth 0.026" in text
    assert "field SFFloat axleTrack 0.135" in text
    assert "field SFFloat chassisLength 0.23" in text
    assert "field SFFloat chassisWidth 0.16" in text
    assert "field SFFloat bodyMass 1.08" in text
    assert "field SFFloat wheelMass 0.06" in text
    assert "field MFVec3f centerOfMass [ 0 0.07 0.01 ]" in text
    # Webots R2025a Physics declares inertiaMatrix as two Vec3 values:
    # principal inertia followed by the three products of inertia.
    assert "field MFVec3f inertiaMatrix" in text
    assert "mass IS bodyMass" in text
    assert "centerOfMass IS centerOfMass" in text
    assert "inertiaMatrix IS inertiaMatrix" in text


def test_physical_proto_has_exact_active_wheel_and_sensor_contract():
    text = _read(PROTO)

    assert len(re.findall(r"\bHingeJoint\s*\{", text)) == 2
    assert len(re.findall(r"\bRotationalMotor\s*\{", text)) == 2
    assert len(re.findall(r"\bPositionSensor\s*\{", text)) == 2
    assert len(re.findall(r"\bDistanceSensor\s*\{", text)) == 3
    assert len(re.findall(r"\bInertialUnit\s*\{", text)) == 1
    assert len(re.findall(r"\bGyro\s*\{", text)) == 1
    assert len(re.findall(r"\bAccelerometer\s*\{", text)) == 1
    assert 'name "left wheel motor"' in text
    assert 'name "right wheel motor"' in text
    assert 'name "left wheel encoder"' in text
    assert 'name "right wheel encoder"' in text
    assert 'name "tof front"' in text
    assert 'name "tof left"' in text
    assert 'name "tof right"' in text
    assert 'name "imu"' in text
    assert 'name "gyro"' in text
    assert 'name "accelerometer"' in text
    assert "field MFVec3f tofLookupTable" in text
    assert text.count("lookupTable IS tofLookupTable") == 3
    assert "0.03 0.03 0" in text
    assert "2 2 0" in text


def test_physical_proto_has_rigid_bodies_collision_and_passive_caster():
    text = _read(PROTO)

    assert len(re.findall(r"\bboundingObject\b", text)) >= 4
    assert len(re.findall(r"\bPhysics\s*\{", text)) >= 4
    assert len(re.findall(r"\bBallJoint\s*\{", text)) == 1
    assert 'name "passive caster"' in text
    assert "DEF WHEEL_MOTION_MARKER PBRAppearance" in text
    assert "appearance USE WHEEL_MOTION_MARKER" in text
    assert "baseColor 0.04 0.82 0.95" in text
    assert "DEF HEADING_MARKER PBRAppearance" in text
    assert "appearance USE HEADING_MARKER" in text
    assert "baseColor 1 0.28 0.03" in text
    assert 'field SFString leftContactMaterial "maze_tire_left"' in text
    assert 'field SFString rightContactMaterial "maze_tire_right"' in text
    assert "contactMaterial IS leftContactMaterial" in text
    assert "contactMaterial IS rightContactMaterial" in text
    assert 'controller "maze_physical_controller"' in text
    assert "supervisor TRUE" in text


def test_physical_worlds_use_eight_ms_step_and_real_collision_surfaces():
    for path in (CALIBRATION_WORLD, MAZE_WORLD):
        text = _read(path)
        assert "EXTERNPROTO \"../protos/PhysicalMazeCar.proto\"" in text
        assert re.search(r"basicTimeStep\s+8\b", text)
        assert re.search(r"FPS\s+24\b", text)
        assert re.search(r"randomSeed\s+20260801\b", text)
        assert 'coordinateSystem "NUE"' in text
        assert "contactProperties [" in text
        assert 'material1 "maze_floor_normal"' in text
        assert 'material2 "maze_tire_left"' in text
        assert 'material2 "maze_tire_right"' in text
        assert 'material2 "maze_tire_left_low"' in text
        assert 'material2 "maze_tire_right_low"' in text
        assert re.search(
            r'material2 "maze_tire_left"\s+'
            r"coulombFriction\s+\[\s*0\.9\s*\]",
            text,
        )
        assert re.search(
            r'material2 "maze_tire_left_low"\s+'
            r"coulombFriction\s+\[\s*0\.25\s*\]",
            text,
        )
        assert not re.search(
            r'material2 "maze_tire_(?:left|right)(?:_low)?"\s+'
            r"coulombFriction\s+\[\s*[0-9.]+\s+[0-9.]+\s*\]",
            text,
        )
        assert 'contactMaterial "maze_floor_normal"' in text
        assert "boundingObject Box" in text
        assert "PhysicalMazeCar {" in text
        assert 'controller "maze_physical_controller"' in text

    calibration = _read(CALIBRATION_WORLD)
    assert 'name "250 mm calibration marker"' in calibration
    assert 'name "90 degree turn marker"' in calibration
    assert re.search(
        r"Viewpoint\s*\{\s*orientation 0 0 1 -0\.5\s*"
        r"position -2\.2 1\.2 0\s+follow \"physical_maze_car\"\s+"
        r'followType "Pan and Tilt Shot"',
        calibration,
    )

    maze = _read(MAZE_WORLD)
    assert re.search(
        r"Viewpoint\s*\{\s*orientation 0 0 1 -0\.5\s*"
        r"position -4\.2 3 0\s+follow \"physical_maze_car\"\s+"
        r'followType "Pan and Tilt Shot"\s+fieldOfView 0\.78',
        maze,
    )
    assert "DEF MAZE_WALLS Group" in maze
    assert "DEF LOW_FRICTION_PATCH Solid" in maze
    assert 'contactMaterial "maze_floor_patch"' in maze


def test_initial_physical_controller_only_observes_and_never_teleports():
    text = _read(CONTROLLER)

    assert "Supervisor()" in text
    assert "MAZE_P1_STABILITY" in text
    assert "simulationQuit(0)" in text
    assert "setSFVec3f" not in text
    assert "setSFRotation" not in text
    assert ".setPosition(" not in text
    assert "world_pose" not in text
