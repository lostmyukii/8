import io
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rdk_maze_tuner.admin import build_parser as build_admin_parser
from rdk_maze_tuner.admin import run_create_user
from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import (
    AuthService,
    LoginRateLimiter,
    PasswordPolicyError,
)
from rdk_maze_tuner.platform.database import Database


TEST_PASSWORD = "correct horse battery staple"


def make_database(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    return database


def make_auth(database, **kwargs):
    from argon2 import PasswordHasher

    return AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
        **kwargs,
    )


def test_auth_uses_argon2_and_stores_only_session_token_digests(tmp_path):
    database = make_database(tmp_path)
    tokens = iter(("session-secret", "csrf-secret"))
    auth = make_auth(
        database,
        token_factory=lambda: next(tokens),
        utc_now=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    )

    user = auth.create_user("operator-a", TEST_PASSWORD)
    credentials = auth.login("operator-a", TEST_PASSWORD)
    principal = auth.resolve_session(credentials.session_token)

    assert principal.user_id == user.user_id
    assert principal.username == "operator-a"
    assert credentials.csrf_token == "csrf-secret"
    assert auth.verify_csrf(principal, credentials.csrf_token) is True

    with database.connection() as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user.user_id,),
        ).fetchone()[0]
        session = connection.execute(
            "SELECT token_digest, csrf_digest FROM sessions WHERE id = ?",
            (principal.session_id,),
        ).fetchone()

    assert password_hash.startswith("$argon2id$")
    assert TEST_PASSWORD not in password_hash
    assert session["token_digest"] != credentials.session_token
    assert session["csrf_digest"] != credentials.csrf_token
    assert len(session["token_digest"]) == 64
    assert len(session["csrf_digest"]) == 64


def test_auth_rejects_weak_password(tmp_path):
    auth = make_auth(make_database(tmp_path))

    with pytest.raises(PasswordPolicyError):
        auth.create_user("operator-a", "short")


def test_expired_session_is_persistently_revoked(tmp_path):
    database = make_database(tmp_path)
    current = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    auth = make_auth(
        database,
        session_ttl_seconds=1,
        utc_now=lambda: current[0],
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    credentials = auth.login("operator-a", TEST_PASSWORD)
    current[0] += timedelta(seconds=2)

    with pytest.raises(RuntimeError, match="expired"):
        auth.resolve_session(credentials.session_token)

    with database.connection() as connection:
        revoked_at = connection.execute(
            "SELECT revoked_at_utc FROM sessions"
        ).fetchone()[0]
    assert revoked_at is not None


def test_http_login_sets_secure_cookie_and_two_clients_have_distinct_sessions(tmp_path):
    database = make_database(tmp_path)
    auth = make_auth(database)
    auth.create_user("operator-a", TEST_PASSWORD)
    auth.create_user("operator-b", TEST_PASSWORD)
    app = create_app(database=database, auth_service=auth)

    with (
        TestClient(app, base_url="https://testserver") as first,
        TestClient(app, base_url="https://testserver") as second,
    ):
        first_login = first.post(
            "/api/auth/login",
            json={"username": "operator-a", "password": TEST_PASSWORD},
        )
        second_login = second.post(
            "/api/auth/login",
            json={"username": "operator-b", "password": TEST_PASSWORD},
        )

        assert first_login.status_code == 200
        assert second_login.status_code == 200
        cookie = first_login.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie
        assert first.cookies.get("maze_session") != second.cookies.get("maze_session")
        assert first.get("/api/state").json()["auth"]["user"]["username"] == "operator-a"
        assert second.get("/api/state").json()["auth"]["user"]["username"] == "operator-b"


def test_unauthenticated_api_is_rejected_and_logout_requires_csrf(tmp_path):
    database = make_database(tmp_path)
    auth = make_auth(database)
    auth.create_user("operator-a", TEST_PASSWORD)
    app = create_app(database=database, auth_service=auth)

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/state").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "operator-a", "password": TEST_PASSWORD},
        )
        csrf_token = login.json()["csrf_token"]

        assert client.post("/api/auth/logout").status_code == 403
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 200
        assert client.get("/api/state").status_code == 401


def test_login_failures_are_rate_limited_without_blocking_other_user(tmp_path):
    database = make_database(tmp_path)
    auth = make_auth(database)
    auth.create_user("operator-a", TEST_PASSWORD)
    auth.create_user("operator-b", TEST_PASSWORD)
    limiter = LoginRateLimiter(
        max_failures=2,
        window_seconds=60,
        block_seconds=60,
        monotonic=lambda: 100.0,
    )
    app = create_app(
        database=database,
        auth_service=auth,
        login_rate_limiter=limiter,
    )

    with TestClient(app, base_url="https://testserver") as client:
        for _ in range(2):
            response = client.post(
                "/api/auth/login",
                json={"username": "operator-a", "password": "wrong-password"},
            )
            assert response.status_code == 401

        blocked = client.post(
            "/api/auth/login",
            json={"username": "operator-a", "password": TEST_PASSWORD},
        )
        other_user = client.post(
            "/api/auth/login",
            json={"username": "operator-b", "password": TEST_PASSWORD},
        )

    assert blocked.status_code == 429
    assert other_user.status_code == 200


def test_admin_create_user_prompts_twice_and_has_no_password_argument(tmp_path):
    parser = build_admin_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "create-user",
                "--username",
                "operator-a",
                "--password",
                "must-not-be-accepted",
            ]
        )

    answers = iter((TEST_PASSWORD, TEST_PASSWORD))
    output = io.StringIO()
    result = run_create_user(
        Namespace(username="operator-a", data_dir=tmp_path),
        getpass_fn=lambda _: next(answers),
        output=output,
    )

    assert result == 0
    assert TEST_PASSWORD not in output.getvalue()
    database = Database(tmp_path / "maze-platform.sqlite3")
    with database.connection() as connection:
        row = connection.execute(
            "SELECT username, password_hash FROM users"
        ).fetchone()
    assert row["username"] == "operator-a"
    assert row["password_hash"].startswith("$argon2id$")
