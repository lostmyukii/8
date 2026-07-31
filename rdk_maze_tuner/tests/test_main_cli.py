import subprocess
import sys

import pytest

from rdk_maze_tuner.main import build_parser


def test_main_py_can_be_executed_directly_for_help():
    result = subprocess.run(
        [sys.executable, "rdk_maze_tuner/main.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "RDK X3 maze car controller" in result.stdout


def test_main_help_exposes_explore_mode_options():
    result = subprocess.run(
        [sys.executable, "rdk_maze_tuner/main.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "--mode {setup,action,explore}" in result.stdout
    assert "--steps STEPS" in result.stdout
    assert "--no-auto-tune" in result.stdout
    assert "--log-file LOG_FILE" in result.stdout
    assert "--export-map EXPORT_MAP" in result.stdout
    assert "--export-params EXPORT_PARAMS" in result.stdout
    assert "--tcp HOST:PORT" in result.stdout


def test_main_requires_exactly_one_transport():
    parser = build_parser()

    assert parser.parse_args(["--serial", "/dev/ttyUSB0"]).serial == "/dev/ttyUSB0"
    assert parser.parse_args(["--tcp", "127.0.0.1:8765"]).tcp == "127.0.0.1:8765"
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--serial", "/dev/ttyUSB0", "--tcp", "127.0.0.1:8765"])
