from __future__ import annotations

import hashlib

import pytest

from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.device_tokens import (
    DeviceAuthenticationError,
    DeviceTokenService,
)


def service(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    return database, DeviceTokenService(
        database=database,
        token_factory=iter(
            (
                "rdk-token-alpha-with-enough-entropy",
                "rdk-token-bravo-with-enough-entropy",
            )
        ).__next__,
    )


def test_device_token_is_bound_and_only_its_digest_is_persisted(tmp_path):
    database, tokens = service(tmp_path)

    issued = tokens.register(device_id="rdk-x3-a", name="maze-rdk")

    assert issued["device_id"] == "rdk-x3-a"
    assert issued["token"] == "rdk-token-alpha-with-enough-entropy"
    with database.connection() as connection:
        row = connection.execute(
            "SELECT token_digest, metadata_json FROM devices WHERE id = ?",
            ("rdk-x3-a",),
        ).fetchone()
    assert row["token_digest"] == hashlib.sha256(
        issued["token"].encode("utf-8")
    ).hexdigest()
    assert issued["token"] not in row["metadata_json"]
    assert tokens.authenticate("rdk-x3-a", issued["token"]).device_id == (
        "rdk-x3-a"
    )
    with pytest.raises(DeviceAuthenticationError):
        tokens.authenticate("other-device", issued["token"])


def test_device_token_can_rotate_and_revoke_without_recovering_old_secret(
    tmp_path,
):
    _database, tokens = service(tmp_path)
    first = tokens.register(device_id="rdk-x3-a", name="maze-rdk")

    second = tokens.rotate("rdk-x3-a")

    assert second["token"] != first["token"]
    with pytest.raises(DeviceAuthenticationError):
        tokens.authenticate("rdk-x3-a", first["token"])
    assert tokens.authenticate(
        "rdk-x3-a",
        second["token"],
    ).device_id == "rdk-x3-a"

    tokens.revoke("rdk-x3-a")
    with pytest.raises(DeviceAuthenticationError):
        tokens.authenticate("rdk-x3-a", second["token"])
