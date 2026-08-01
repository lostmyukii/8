from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
    LeaseUnavailableError,
)
from rdk_maze_tuner.platform.database import Database


TEST_PASSWORD = "correct horse battery staple"


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def make_services(tmp_path):
    from argon2 import PasswordHasher

    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    clock = MutableClock()
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
        utc_now=clock.now,
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    auth.create_user("operator-b", TEST_PASSWORD)
    first_credentials = auth.login("operator-a", TEST_PASSWORD)
    second_credentials = auth.login("operator-b", TEST_PASSWORD)
    first = auth.resolve_session(first_credentials.session_token)
    second = auth.resolve_session(second_credentials.session_token)
    lease_tokens = iter(("lease-a", "lease-b", "lease-c"))
    leases = ControlLeaseService(
        database=database,
        utc_now=clock.now,
        token_factory=lambda: next(lease_tokens),
    )
    return database, clock, auth, leases, first, second


def test_control_lease_claim_heartbeat_release_and_expiry(tmp_path):
    database, clock, _, leases, first, second = make_services(tmp_path)

    grant = leases.claim(first)
    assert grant.lease_token == "lease-a"
    assert grant.renew_after_seconds == 5
    assert grant.lease_seconds == 15
    with pytest.raises(LeaseUnavailableError):
        leases.claim(second)

    clock.advance(5)
    renewed = leases.heartbeat(first, grant.lease_token)
    assert renewed.remaining_seconds == 15
    leases.require_holder(first, grant.lease_token)
    with pytest.raises(LeasePermissionError):
        leases.require_holder(second, grant.lease_token)

    leases.release(first, grant.lease_token)
    second_grant = leases.claim(second)
    assert second_grant.lease_token == "lease-b"

    clock.advance(16)
    first_again = leases.claim(first)
    assert first_again.lease_token == "lease-c"
    assert leases.status()["holder"]["username"] == "operator-a"

    with database.connection() as connection:
        audit_types = [
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            )
        ]
    assert "control.claimed" in audit_types
    assert "control.heartbeat" in audit_types
    assert "control.released" in audit_types
    assert "control.expired" in audit_types


def test_expired_lease_rejection_persists_clear_and_audit(tmp_path):
    database, clock, _, leases, first, _ = make_services(tmp_path)
    grant = leases.claim(first)
    clock.advance(16)

    with pytest.raises(LeasePermissionError, match="expired"):
        leases.require_holder(first, grant.lease_token)

    with database.connection() as connection:
        lease_row = connection.execute(
            "SELECT holder_user_id, lease_token_digest FROM control_lease"
        ).fetchone()
        expired_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE event_type = 'control.expired'
            """
        ).fetchone()[0]
    assert lease_row["holder_user_id"] is None
    assert lease_row["lease_token_digest"] is None
    assert expired_count == 1


def login(client, username):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_two_http_users_share_view_but_only_holder_controls_and_both_estop(tmp_path):
    database, _, auth, leases, _, _ = make_services(tmp_path)
    app = create_app(
        database=database,
        auth_service=auth,
        control_lease_service=leases,
    )

    with (
        TestClient(app, base_url="https://testserver") as first,
        TestClient(app, base_url="https://testserver") as second,
    ):
        first_csrf = login(first, "operator-a")
        second_csrf = login(second, "operator-b")
        claim = first.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": first_csrf},
        )
        assert claim.status_code == 200
        lease_token = claim.json()["lease_token"]

        denied_claim = second.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": second_csrf},
        )
        assert denied_claim.status_code == 409
        assert "session_id" not in denied_claim.text
        assert first.get("/api/state").status_code == 200
        assert second.get("/api/state").status_code == 200
        assert second.get("/api/control/status").json()["role"] == "viewer"

        denied_requests = [
            second.post(
                "/api/command/stop",
                json={"reason": "viewer"},
                headers={"X-CSRF-Token": second_csrf},
            ),
            second.post(
                "/api/command/action",
                json={"name": "move_cell"},
                headers={"X-CSRF-Token": second_csrf},
            ),
            second.post(
                "/api/params",
                json={"updates": {"motor.base_speed": 0.26}},
                headers={"X-CSRF-Token": second_csrf},
            ),
            second.post(
                "/api/auto-tune",
                json={"enabled": False},
                headers={"X-CSRF-Token": second_csrf},
            ),
        ]
        assert [response.status_code for response in denied_requests] == [403] * 4

        estop = second.post(
            "/api/command/estop",
            json={"reason": "viewer-safety"},
            headers={"X-CSRF-Token": second_csrf},
        )
        holder_stop = first.post(
            "/api/command/stop",
            json={"reason": "holder"},
            headers={
                "X-CSRF-Token": first_csrf,
                "X-Control-Lease": lease_token,
            },
        )
        heartbeat = first.post(
            "/api/control/heartbeat",
            headers={
                "X-CSRF-Token": first_csrf,
                "X-Control-Lease": lease_token,
            },
        )

    assert estop.status_code == 200
    assert holder_stop.status_code == 200
    assert heartbeat.status_code == 200

    with database.connection() as connection:
        operation_rows = [
            row
            for row in connection.execute(
                """
                SELECT event_type, details_json
                FROM audit_events
                WHERE event_type IN ('control.estop', 'control.stop')
                ORDER BY id
                """
            )
        ]
    assert [row["event_type"] for row in operation_rows] == [
        "control.estop",
        "control.stop",
    ]
    assert all("viewer-safety" not in row["details_json"] for row in operation_rows)
    assert all('"holder"' not in row["details_json"] for row in operation_rows)


def test_control_state_changes_require_csrf_and_current_lease_token(tmp_path):
    database, _, auth, leases, _, _ = make_services(tmp_path)
    app = create_app(
        database=database,
        auth_service=auth,
        control_lease_service=leases,
    )

    with TestClient(app, base_url="https://testserver") as client:
        csrf = login(client, "operator-a")
        assert client.post("/api/control/claim").status_code == 403
        claim = client.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": csrf},
        )
        assert claim.status_code == 200
        assert (
            client.post(
                "/api/command/stop",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/command/estop",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
