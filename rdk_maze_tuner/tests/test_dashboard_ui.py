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
        self.elements: dict[str, tuple[str, dict[str, str]]] = {}
        self.labels_for: set[str] = set()
        self.aria_live_ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
            self.elements[element_id] = (tag, attributes)
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
        "simulationViewer",
        "streamPlaceholder",
        "taskState",
        "taskResetButton",
        "taskStartButton",
        "taskPauseButton",
        "stopButton",
        "automaticGoalX",
        "automaticGoalY",
        "mapGoalCandidates",
        "mapGoalDigest",
        "mapGoalPathLength",
        "mapGoalStatus",
        "debugGoalX",
        "debugGoalY",
        "debugCoordinateButton",
        "manualGoalNotice",
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
    assert {"goalX", "goalY"} & parser.ids == set()
    assert parser.elements["automaticGoalX"][0] == "output"
    assert parser.elements["automaticGoalY"][0] == "output"
    assert "disabled" in parser.elements["debugCoordinateButton"][1]


def test_automatic_goal_panel_is_readonly_and_debug_goal_is_isolated(
    tmp_path,
):
    html = dashboard_html(tmp_path)

    assert "地图终点（自动）" in html
    assert "不会改变自动终点，也不会触发自动完成" in html
    assert "坐标单步调试尚未接入" in html
    assert 'id="estopButton"' in html


def test_dashboard_embeds_authenticated_same_origin_simulation_viewer(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    auth = AuthService(database=database)
    auth.create_user("operator-a", "correct horse battery staple")
    app = create_app(database=database, auth_service=auth)
    parser = DashboardContractParser()
    parser.feed(TestClient(app).get("/").text)

    tag, attributes = parser.elements["simulationViewer"]
    assert tag == "iframe"
    assert attributes["data-src"] == "/simulation-viewer"
    assert attributes["src"] == "about:blank"
    assert attributes["title"] == "Webots 实时物理仿真"
    assert "allow-scripts" in attributes["sandbox"]

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/simulation-viewer").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator-a",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200
        viewer = client.get("/simulation-viewer")

    assert viewer.status_code == 200
    assert '<webots-view id="webotsView">' in viewer.text
    assert "R2025a/WebotsView.js" in viewer.text
    assert "/static/simulation_viewer.js" in viewer.text


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
        "stream.js",
        "simulation_viewer.js",
    ):
        assert (STATIC_DIR / filename).is_file()

    entrypoint = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'from "./api.js"' in entrypoint
    assert 'from "./state.js"' in entrypoint
    assert 'from "./render.js"' in entrypoint
    assert 'from "./controls.js"' in entrypoint
    assert 'from "./replay.js"' in entrypoint
    assert 'from "./stream.js"' in entrypoint


def test_webots_viewer_auto_connects_with_same_origin_secure_websocket():
    stream_source = (STATIC_DIR / "stream.js").read_text(encoding="utf-8")
    viewer_source = (STATIC_DIR / "simulation_viewer.js").read_text(
        encoding="utf-8"
    )

    assert 'iframe.dataset.src' in stream_source
    assert 'iframe.src = "about:blank"' in stream_source
    assert 'selectedMode === "simulation"' in stream_source
    assert 'event.origin !== window.location.origin' in stream_source
    assert 'location.protocol === "https:" ? "wss:" : "ws:"' in viewer_source
    assert 'location.host' in viewer_source
    assert 'const STREAM_PATH = "/simulation/"' in viewer_source
    assert '.connect(' in viewer_source
    assert '"w3d"' in viewer_source
    assert '"maze.webots.ready"' in viewer_source
    assert ".close()" in viewer_source


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


def test_dashboard_reset_rebuilds_changed_task_and_blocks_failed_preflight():
    controls_source = (STATIC_DIR / "controls.js").read_text(
        encoding="utf-8"
    )
    render_source = (STATIC_DIR / "render.js").read_text(encoding="utf-8")
    state_source = (STATIC_DIR / "state.js").read_text(encoding="utf-8")
    api_source = (STATIC_DIR / "api.js").read_text(encoding="utf-8")

    assert (
        "function taskDefinitionChanged(task, definition)"
        in controls_source
    )
    for field in (
        "map_version",
        "param_version",
        "physical_profile_id",
    ):
        assert f"task.{field} !== definition.{field}" in controls_source
    assert 'run_kind: "auto_to_map_goal"' in controls_source
    assert "task.goal?.cell" not in controls_source
    assert '$("goalX")' not in controls_source
    assert '$("goalY")' not in controls_source
    assert "getMapVersion" in controls_source
    assert "setMapVersionLoading" in controls_source
    assert "setMapVersionDetail" in controls_source
    assert "setMapVersionError" in controls_source
    assert "assertAutomaticMapReady" in controls_source
    assert "export function getMapVersion" in api_source
    assert "mapVersionStatus" in state_source
    assert "selectedMapVersionId" in state_source
    assert "mapGoal" in state_source
    assert "mapGoalReady" in render_source
    assert "preflight?.preflight?.ok !== true" in controls_source
    assert "preflight?.preflight?.message" in controls_source
    assert "blockedPreflight" in render_source
    assert 'status === "PREFLIGHT"' in render_source


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
