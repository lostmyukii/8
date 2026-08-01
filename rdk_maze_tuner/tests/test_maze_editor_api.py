from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.map_repository import MapRepository


TEST_PASSWORD = "correct horse battery staple"
STATIC_DIR = Path("rdk_maze_tuner/dashboard/static")


def definition_payload(*, goal_x: int = 1) -> dict:
    return {
        "rows": 2,
        "cols": 2,
        "cell_width_mm": 300,
        "cell_height_mm": 300,
        "wall_thickness_mm": 18,
        "wall_height_mm": 120,
        "start": {"x": 0, "y": 1, "heading": "N"},
        "goals": [{"x": goal_x, "y": 0}],
        "walls": [
            {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
            {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
            {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
            {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
        ],
        "source_image_digest": None,
    }


def authenticated_client(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    repository = MapRepository(
        database=database,
        artifacts_dir=tmp_path / "artifacts",
    )
    client = TestClient(
        create_app(
            database=database,
            auth_service=auth,
            map_repository=repository,
        ),
        base_url="https://testserver",
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "operator-a", "password": TEST_PASSWORD},
    )
    csrf = login.json()["csrf_token"]
    claim = client.post(
        "/api/control/claim",
        headers={"X-CSRF-Token": csrf},
    )
    client.headers.update(
        {
            "X-CSRF-Token": csrf,
            "X-Control-Lease": claim.json()["lease_token"],
        }
    )
    return client, database, repository


def test_map_api_requires_login_and_control_for_writes(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    app = create_app(
        database=database,
        map_repository=MapRepository(
            database=database,
            artifacts_dir=tmp_path / "artifacts",
        ),
    )
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/api/maps").status_code == 401
    assert client.post(
        "/api/maps",
        json={"name": "实验迷宫", "definition": definition_payload()},
    ).status_code == 401


def test_map_api_saves_immutable_versions_and_preserves_old_definition(tmp_path):
    client, _database, _repository = authenticated_client(tmp_path)

    created = client.post(
        "/api/maps",
        json={"name": "实验迷宫", "definition": definition_payload()},
    )
    assert created.status_code == 201
    first = created.json()["version"]
    map_id = created.json()["map"]["map_id"]
    assert first["version_number"] == 1

    updated_definition = definition_payload(goal_x=0)
    updated = client.post(
        f"/api/maps/{map_id}/versions",
        json={"definition": updated_definition},
    )
    assert updated.status_code == 201
    second = updated.json()["version"]
    assert second["version_number"] == 2
    assert second["version_id"] != first["version_id"]
    assert second["digest"] != first["digest"]

    original = client.get(
        f"/api/map-versions/{first['version_id']}"
    ).json()
    latest = client.get(
        f"/api/map-versions/{second['version_id']}"
    ).json()
    assert original["definition"]["goals"] == [{"x": 1, "y": 0}]
    assert latest["definition"]["goals"] == [{"x": 0, "y": 0}]
    maps = client.get("/api/maps").json()["maps"]
    assert maps[0]["latest_version"]["version_id"] == second["version_id"]
    versions = client.get(
        f"/api/maps/{map_id}/versions"
    ).json()["versions"]
    assert [
        version["version_id"] for version in versions
    ] == [second["version_id"], first["version_id"]]


def test_map_api_stores_source_image_as_digest_addressed_artifact(tmp_path):
    client, database, repository = authenticated_client(tmp_path)
    created = client.post(
        "/api/maps",
        json={"name": "照片描摹", "definition": definition_payload()},
    ).json()
    map_id = created["map"]["map_id"]

    response = client.post(
        f"/api/maps/{map_id}/source-image?filename=maze.png",
        content=b"\x89PNG\r\n\x1a\npreview-bytes",
        headers={"Content-Type": "image/png"},
    )

    assert response.status_code == 201
    artifact = response.json()["artifact"]
    assert len(artifact["sha256"]) == 64
    artifact_path = repository.artifacts_dir / artifact["relative_path"]
    assert artifact_path.read_bytes().endswith(b"preview-bytes")
    with database.connection() as connection:
        stored = connection.execute(
            "SELECT kind, sha256 FROM artifacts WHERE id = ?",
            (artifact["artifact_id"],),
        ).fetchone()
    assert stored["kind"] == "map_source_image"
    assert stored["sha256"] == artifact["sha256"]


def test_maze_editor_dom_and_modules_cover_drawing_and_version_workflow(tmp_path):
    client, _database, _repository = authenticated_client(tmp_path)

    html = client.get("/").text
    for element_id in (
        "mazeEditor",
        "mazeCanvas",
        "mazeToolDraw",
        "mazeToolErase",
        "mazeUndo",
        "mazeRedo",
        "mazeSetStart",
        "mazeSetGoal",
        "mazeHeading",
        "mazeImageInput",
        "mazeCalibrationLength",
        "mazeSaveVersion",
        "mazeVersionList",
    ):
        assert f'id="{element_id}"' in html
    assert "/static/maze_editor.css" in html

    editor_source = (STATIC_DIR / "maze_editor.js").read_text(
        encoding="utf-8"
    )
    for capability in (
        "pointerdown",
        "pointermove",
        "pointerup",
        "undo",
        "redo",
        "source_image_digest",
        "saveMapVersion",
        "listMapVersions",
    ):
        assert capability in editor_source
    api_source = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    assert "export function listMapVersions" in api_source
    entrypoint = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'from "./maze_editor.js"' in entrypoint
