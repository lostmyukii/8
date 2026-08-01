import json
from pathlib import Path

import pytest

from simulation.webots.maze_car.tools.physical_acceptance_schema import (
    AcceptanceReportError,
    validate_acceptance_report,
)
from simulation.webots.maze_car.tools.run_physical_acceptance import (
    AcceptanceRunConfig,
    ManagedWebotsProcess,
    PhysicalAcceptanceRunner,
    drain_frames,
    exit_code_for_report,
    scenario_resets_between_trials,
)


class FakeProcess:
    def __init__(self, *, pid: int = 4102, returncode=None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


def complete_report() -> dict:
    return {
        "schema_version": 1,
        "run_id": "physical-acceptance-test",
        "status": "PASS",
        "source_commit": "a" * 40,
        "webots_version": "R2025a",
        "started_at_utc": "2026-08-01T00:00:00Z",
        "ended_at_utc": "2026-08-01T00:01:00Z",
        "output_dir": "physical-acceptance-test",
        "profiles": [
            {
                "profile_id": "normal-v1",
                "digest": "b" * 64,
                "seed": 20260801,
            }
        ],
        "maps": [
            {
                "map_version_id": "builtin-calibration-3x3",
                "digest": "c" * 64,
            }
        ],
        "p1": {
            "status": "PASS",
            "metrics": {"horizontal_drift_m": 0.0},
            "thresholds": {"horizontal_drift_m": {"passed": True}},
        },
        "p2": {
            "status": "PASS",
            "metrics": {"max_tof_error_mm": 0.0},
            "thresholds": {"max_tof_error_mm": {"passed": True}},
        },
        "scenarios": [
            {
                "scenario_id": "normal-p3-v1",
                "status": "PASS",
                "profile_id": "normal-v1",
                "profile_digest": "b" * 64,
                "map_version_id": "builtin-calibration-3x3",
                "map_digest": "c" * 64,
                "seed": 20260801,
                "metrics": {"success_rate": 1.0},
                "thresholds": {
                    "min_success_rate": {"passed": True}
                },
                "errors": [],
            }
        ],
        "performance": {
            "real_time_factor": 0.92,
            "controller_period_ms": 8.0,
        },
        "errors": [],
        "artifacts": {
            "events_jsonl": "events.jsonl",
            "report_json": "report.json",
        },
    }


def test_acceptance_report_schema_rejects_missing_or_incomplete_fields():
    report = complete_report()

    assert validate_acceptance_report(report) == report
    for field in (
        "source_commit",
        "webots_version",
        "profiles",
        "maps",
        "p1",
        "p2",
        "scenarios",
        "performance",
        "errors",
        "artifacts",
    ):
        incomplete = dict(report)
        incomplete.pop(field)
        with pytest.raises(AcceptanceReportError):
            validate_acceptance_report(incomplete)


def test_managed_process_only_terminates_the_process_it_started(tmp_path):
    owned = FakeProcess(pid=9912)
    unrelated = FakeProcess(pid=9913)
    started: list[tuple[list[str], dict]] = []

    def popen(command, **kwargs):
        started.append((command, kwargs))
        return owned

    pid_file = tmp_path / "owned.pid"
    with ManagedWebotsProcess(
        command=["/usr/local/bin/webots", "--batch", "world.wbt"],
        environment={"MAZE_SIM_PORT": "41000"},
        pid_file=pid_file,
        popen=popen,
    ) as process:
        assert process is owned
        assert pid_file.read_text(encoding="utf-8") == "9912\n"
        assert started[0][0][-1] == "world.wbt"
        assert unrelated.terminated is False

    assert owned.terminated is True
    assert unrelated.terminated is False
    assert not pid_file.exists()


def test_fast_mode_telemetry_drain_is_bounded():
    class EndlessTelemetry:
        def get(self, *, timeout_s):
            assert timeout_s == 0.0
            return {
                "type": "telemetry",
                "ts_ms": 8,
                "sim_truth": {
                    "x_mm": 0,
                    "y_mm": 0,
                    "yaw_deg": 0,
                },
            }

    frames: list[dict] = []
    recorded: list[dict] = []

    drain_frames(
        EndlessTelemetry(),
        frames,
        event_sink=lambda _kind, payload: recorded.append(
            dict(payload)
        ),
    )

    assert len(frames) == len(recorded) == 256


def test_repeatability_scenarios_reset_but_patch_route_stays_continuous():
    assert scenario_resets_between_trials("normal-p3-v1") is True
    assert scenario_resets_between_trials("low-p4-v1") is True
    assert scenario_resets_between_trials("asymmetric-p4-v1") is True
    assert scenario_resets_between_trials("local-patch-p4-v1") is False


def test_missing_webots_returns_unavailable_without_fake_pass(tmp_path):
    config = AcceptanceRunConfig(
        webots=tmp_path / "missing-webots",
        world=Path(
            "simulation/webots/maze_car/worlds/"
            "maze_physical_calibration.wbt"
        ),
        scenarios=Path(
            "simulation/webots/maze_car/config/"
            "acceptance_scenarios.yaml"
        ),
        output=tmp_path / "acceptance",
        total_timeout_s=3.0,
    )
    runner = PhysicalAcceptanceRunner(config)

    report = runner.run()

    assert report["status"] == "unavailable"
    assert exit_code_for_report(report) != 0
    assert report["errors"][0]["code"] == "WEBOTS_UNAVAILABLE"
    report_path = (
        config.output / report["run_id"] / "report.json"
    )
    events_path = (
        config.output / report["run_id"] / "events.jsonl"
    )
    assert report_path.is_file()
    assert events_path.is_file()
    assert json.loads(
        report_path.read_text(encoding="utf-8")
    )["status"] == "unavailable"


def test_runner_uses_temporary_port_directory_and_writes_complete_report(
    tmp_path,
):
    webots = tmp_path / "webots"
    webots.write_text("#!/bin/sh\n", encoding="utf-8")
    webots.chmod(0o755)
    config = AcceptanceRunConfig(
        webots=webots,
        world=Path(
            "simulation/webots/maze_car/worlds/"
            "maze_physical_calibration.wbt"
        ),
        scenarios=Path(
            "simulation/webots/maze_car/config/"
            "acceptance_scenarios.yaml"
        ),
        output=tmp_path / "acceptance",
        total_timeout_s=30.0,
    )
    used_ports: list[int] = []
    used_workdirs: list[Path] = []

    def execute_scenario(scenario, *, port, work_dir, event_sink):
        used_ports.append(port)
        used_workdirs.append(work_dir)
        event_sink(
            "ready",
            {
                "scenario_id": scenario.scenario_id,
                "profile_id": scenario.physical_profile_id,
            },
        )
        return {
            "scenario_id": scenario.scenario_id,
            "status": "PASS",
            "profile_id": scenario.physical_profile_id,
            "profile_digest": scenario.physical_profile_digest,
            "map_version_id": scenario.map_version_id,
            "map_digest": scenario.map_digest,
            "seed": scenario.seed,
            "metrics": {
                "success_rate": 1.0,
                "controller_period_ms": 8.0,
                "real_time_factor": 1.0,
            },
            "thresholds": {
                name: {"passed": True, "actual": value}
                for name, value in scenario.acceptance_thresholds
            },
            "errors": [],
            "initial_frames": (
                [
                    {
                        "front_mm": 536,
                        "left_mm": 574,
                        "right_mm": 574,
                    },
                    {
                        "front_mm": 536,
                        "left_mm": 574,
                        "right_mm": 574,
                    },
                ]
                if scenario.physical_profile_id == "normal-v1"
                else []
            ),
        }

    runner = PhysicalAcceptanceRunner(
        config,
        reserve_port=iter((41001, 41002, 41003, 41004)).__next__,
        scenario_executor=execute_scenario,
        stability_executor=lambda **_kwargs: {
            "status": "PASS",
            "metrics": {"horizontal_drift_m": 0.0},
            "thresholds": {
                "horizontal_drift_m": {"passed": True}
            },
        },
        webots_version=lambda _path: "R2025a",
        source_commit=lambda: "d" * 40,
    )

    report = runner.run()

    assert report["status"] == "PASS"
    assert exit_code_for_report(report) == 0
    assert len(set(used_ports)) == 4
    assert all(port != 8765 for port in used_ports)
    assert all(
        path.name.startswith(".tmp-")
        for path in used_workdirs
    )
    run_dir = config.output / report["run_id"]
    assert run_dir.is_dir()
    assert not any(
        path.name.startswith(".tmp-")
        for path in config.output.iterdir()
    )
    assert (run_dir / "events.jsonl").is_file()
    assert validate_acceptance_report(
        json.loads((run_dir / "report.json").read_text())
    )["status"] == "PASS"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ready timeout"),
        RuntimeError("protocol disconnected"),
        TimeoutError("overall timeout"),
    ],
)
def test_runner_failures_are_nonzero_and_never_emit_pass(
    tmp_path,
    failure,
):
    webots = tmp_path / "webots"
    webots.write_text("#!/bin/sh\n", encoding="utf-8")
    webots.chmod(0o755)
    config = AcceptanceRunConfig(
        webots=webots,
        world=Path(
            "simulation/webots/maze_car/worlds/"
            "maze_physical_calibration.wbt"
        ),
        scenarios=Path(
            "simulation/webots/maze_car/config/"
            "acceptance_scenarios.yaml"
        ),
        output=tmp_path / "acceptance",
        total_timeout_s=1.0,
    )

    def fail_scenario(*_args, **_kwargs):
        raise failure

    runner = PhysicalAcceptanceRunner(
        config,
        scenario_executor=fail_scenario,
        stability_executor=lambda **_kwargs: {
            "status": "PASS",
            "metrics": {},
            "thresholds": {},
        },
        webots_version=lambda _path: "R2025a",
        source_commit=lambda: "e" * 40,
    )

    report = runner.run()

    assert report["status"] == "FAIL"
    assert exit_code_for_report(report) != 0
    assert report["errors"]
    assert report["status"] != "PASS"


def test_systemd_services_use_physical_world_and_private_protocol():
    systemd = Path("deploy/server/systemd")
    for name in (
        "maze-webots-stream.service",
        "maze-webots-desktop.service",
        "maze-webots-headless.service",
    ):
        source = (systemd / name).read_text(encoding="utf-8")
        assert "maze_physical_world.wbt" in source
        assert "MAZE_PHYSICAL_PROFILE_DIR" in source
        assert "MAZE_DEFAULT_PHYSICAL_PROFILE=normal-v1" in source
        assert "MAZE_PHYSICAL_PROFILE_ID=normal-v1" in source
        assert "MAZE_SIM_HOST=127.0.0.1" in source
        assert "MAZE_SIM_PORT=8765" in source
        assert "Conflicts=" in source
    stream = (
        systemd / "maze-webots-stream.service"
    ).read_text(encoding="utf-8")
    headless = (
        systemd / "maze-webots-headless.service"
    ).read_text(encoding="utf-8")
    assert "--stream=w3d" in stream
    assert "--no-rendering" in headless
    dashboard = (
        systemd / "maze-dashboard.service"
    ).read_text(encoding="utf-8")
    assert "MAZE_ENV=production" in dashboard
    assert "MAZE_DATA_DIR=/srv/maze/shared" in dashboard


def test_deploy_requires_acceptance_before_atomic_switch_and_exact_rollback():
    deploy = Path("deploy/server/deploy_release.sh").read_text(
        encoding="utf-8"
    )
    rollback = Path("deploy/server/rollback_release.sh").read_text(
        encoding="utf-8"
    )
    mode = Path("deploy/server/maze-sim-mode").read_text(
        encoding="utf-8"
    )

    assert "run_physical_acceptance" in deploy
    assert 'cd "${release_dir}"' in deploy
    assert deploy.index("run_physical_acceptance") < deploy.index(
        'mv -Tf "${candidate_link}" "${current_link}"'
    )
    assert "pytest" in deploy
    assert ".venv/bin/pio" in deploy
    assert '" run' in deploy
    assert "node --input-type=module --check" in deploy
    assert "wait_tcp 127.0.0.1 8765" in deploy
    assert "require_loopback_listener" in deploy
    assert "chmod -R a-w" in deploy
    assert "pkill" not in deploy
    assert "killall" not in deploy

    assert "release_id=${1:-}" in rollback
    assert "/srv/maze/releases/${release_id}" in rollback
    assert "pkill" not in rollback
    assert "killall" not in rollback
    assert "maze_physical_world.wbt" in mode


def test_caddy_keeps_simulation_behind_platform_authentication():
    caddyfile = Path("deploy/server/Caddyfile").read_text(encoding="utf-8")

    assert "8.ilelezhan.cn" in caddyfile
    assert "@simulation path /simulation /simulation/*" in caddyfile
    assert "forward_auth 127.0.0.1:8000" in caddyfile
    assert "uri /api/auth/authorize" in caddyfile
    assert "uri strip_prefix /simulation" in caddyfile
    assert "reverse_proxy 127.0.0.1:1234" in caddyfile
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "6080" not in caddyfile
    assert "5901" not in caddyfile
    assert "admin off" not in caddyfile


def test_release_deployment_validates_caddy_and_local_https_vhost():
    deploy = Path("deploy/server/deploy_release.sh").read_text(
        encoding="utf-8"
    )

    assert "caddy validate --config /etc/caddy/Caddyfile" in deploy
    assert "systemctl reload caddy.service" in deploy
    assert "--resolve" in deploy
    assert "127.0.0.1" in deploy

    installer = Path("deploy/server/install_host.sh").read_text(
        encoding="utf-8"
    )
    assert "ufw allow 80/tcp" in installer
    assert "ufw allow 443/tcp" in installer
