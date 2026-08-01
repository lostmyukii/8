from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database


STATIC_DIR = Path("rdk_maze_tuner/dashboard/static")


class DashboardContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[dict[str, str]] = []
        self.labels_for: set[str] = set()
        self.aria_live_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
            if attributes.get("aria-live"):
                self.aria_live_ids.add(element_id)
        if tag == "script":
            self.scripts.append(attributes)
        if tag == "label" and attributes.get("for"):
            self.labels_for.add(attributes["for"])


def dashboard_html(tmp_path) -> str:
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    app = create_app(
        database=database,
        auth_service=AuthService(database=database),
    )
    return TestClient(app).get("/").text


def test_dashboard_v2_has_complete_mission_control_dom_contract(tmp_path):
    parser = DashboardContractParser()
    parser.feed(dashboard_html(tmp_path))

    required_ids = {
        "loginGate",
        "loginForm",
        "appShell",
        "modeSimulation",
        "modeReal",
        "leaseState",
        "claimControlButton",
        "estopButton",
        "liveViewport",
        "streamFrame",
        "taskState",
        "taskResetButton",
        "taskStartButton",
        "taskPauseButton",
        "stopButton",
        "cellPosition",
        "continuousPose",
        "headingValue",
        "poseConfidence",
        "poseCovariance",
        "imuState",
        "slipPair",
        "paramWorkbench",
        "mazeMap",
        "eventTimeline",
        "runReplay",
        "scoreTotal",
        "scoreBreakdown",
        "replayVideo",
        "replayStructuredFallback",
        "replayPlayButton",
        "replaySeek",
        "replayRail",
        "replayKeyEvents",
        "replayEventDetail",
        "replayRunSelect",
        "physicalProfileInput",
        "physicalProfileId",
        "physicalProfileDigest",
        "physicalRandomSeed",
        "physicalWebotsVersion",
        "physicalMass",
        "physicalCenterOfMass",
        "physicalWheelGeometry",
        "physicalSurface",
        "wheelEvidence",
        "tofEvidence",
        "imuEvidence",
        "controlEvidence",
        "slipEstimateEvidence",
        "truthEvaluationCard",
        "truthSlipEvidence",
        "poseComparisonEvidence",
        "actionCompletionEvidence",
        "safetyEvidence",
        "lastPhysicalError",
    }

    assert required_ids <= parser.ids
    assert {"username", "password"} <= parser.labels_for
    assert {"globalNotice", "taskState"} <= parser.aria_live_ids


def test_dashboard_v2_loads_small_native_javascript_modules(tmp_path):
    parser = DashboardContractParser()
    parser.feed(dashboard_html(tmp_path))

    assert {
        "src": "/static/app.js",
        "type": "module",
    } in parser.scripts
    for filename in (
        "api.js",
        "state.js",
        "render.js",
        "controls.js",
        "replay.js",
    ):
        assert (STATIC_DIR / filename).is_file()

    entrypoint = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'from "./api.js"' in entrypoint
    assert 'from "./state.js"' in entrypoint
    assert 'from "./render.js"' in entrypoint
    assert 'from "./controls.js"' in entrypoint
    assert 'from "./replay.js"' in entrypoint


def test_dashboard_controls_cover_auth_lease_tasks_and_shared_estop():
    api_source = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    controls_source = (STATIC_DIR / "controls.js").read_text(
        encoding="utf-8"
    )
    render_source = (STATIC_DIR / "render.js").read_text(encoding="utf-8")

    for endpoint in (
        "/api/auth/login",
        "/api/auth/logout",
        "/api/control/claim",
        "/api/control/heartbeat",
        "/api/control/release",
        "/api/tasks",
    ):
        assert endpoint in api_source
    for operation in ("preflight", "reset", "start", "pause", "stop"):
        assert f'"{operation}"' in controls_source
    assert "/api/command/estop" in api_source
    assert "control.role === \"controller\"" in render_source
    assert "disabled" in render_source


def test_dashboard_replay_uses_scored_monotonic_timeline_contract():
    api_source = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    replay_source = (STATIC_DIR / "replay.js").read_text(encoding="utf-8")

    for suffix in ("", "/events", "/replay"):
        assert f"${{encoded}}{suffix}" in api_source
    assert "duration_ms" in replay_source
    assert "key_events" in replay_source
    assert "currentTime" in replay_source
    assert "结构化回放可用" in replay_source
    assert "listRuns" in replay_source
    for channel in (
        "physical_profile",
        "wheel",
        "tof",
        "imu",
        "control",
        "slip_estimate",
        "sim_truth",
        "surface",
        "fault",
    ):
        assert channel in replay_source


def test_dashboard_profile_selection_is_run_scoped_and_truth_is_labeled():
    api_source = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    controls_source = (STATIC_DIR / "controls.js").read_text(
        encoding="utf-8"
    )
    render_source = (STATIC_DIR / "render.js").read_text(encoding="utf-8")
    state_source = (STATIC_DIR / "state.js").read_text(encoding="utf-8")

    assert "/api/physical-profiles" in api_source
    assert "physical_profile_id" in controls_source
    assert "physical_profile_id" in render_source
    assert "truth_evaluation_only" in render_source
    assert "仅评估" in render_source
    assert "selectedPhysicalProfileId" in state_source


def test_dashboard_css_has_desktop_laptop_and_readonly_narrow_layouts():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    assert "@media (max-width: 1280px)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 768px)" in css
    assert ".heading-instrument" in css
    assert ".estop-button" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
