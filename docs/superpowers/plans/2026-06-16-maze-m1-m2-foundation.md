# Maze M1/M2 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first verifiable foundation for the maze car: RDK-side newline JSON protocol, parameter validation, maze mapping/planning core, and ESP32 PlatformIO firmware skeleton aligned with `DEVELOPMENT.md`.

**Architecture:** Keep old top-level prototypes untouched and create new `rdk_maze_tuner/` and `esp32_firmware/` directories. RDK code is testable without hardware through pure Python modules and fake frames; ESP32 code is structured around JSON serial commands and a non-blocking motion controller.

**Tech Stack:** Python 3.9 stdlib, pytest when available, YAML through PyYAML when available with JSON fallback, PlatformIO Arduino framework for ESP32, ArduinoJson for ESP32 JSON parsing.

---

### Task 1: RDK Protocol And Parameter Foundation

**Files:**
- Create: `rdk_maze_tuner/__init__.py`
- Create: `rdk_maze_tuner/core/__init__.py`
- Create: `rdk_maze_tuner/core/protocol.py`
- Create: `rdk_maze_tuner/core/param_manager.py`
- Create: `rdk_maze_tuner/config/params.yaml`
- Create: `rdk_maze_tuner/config/limits.yaml`
- Test: `rdk_maze_tuner/tests/test_protocol.py`
- Test: `rdk_maze_tuner/tests/test_param_manager.py`

- [ ] **Step 1: Write failing protocol tests**

```python
from rdk_maze_tuner.core.protocol import (
    ProtocolError,
    build_action,
    build_heartbeat,
    build_set_params,
    decode_line,
    encode_message,
)


def test_encode_message_appends_newline_and_keeps_compact_json():
    assert encode_message({"type": "heartbeat", "seq": 1}) == b'{"type":"heartbeat","seq":1}\n'


def test_decode_line_rejects_non_object_payload():
    try:
        decode_line(b"[1, 2]\n")
    except ProtocolError as exc:
        assert "object" in str(exc)
    else:
        raise AssertionError("expected ProtocolError")


def test_builders_create_documented_messages():
    assert build_heartbeat(seq=7, ts_ms=123) == {"type": "heartbeat", "seq": 7, "ts_ms": 123}
    assert build_set_params(seq=8, params={"base_speed": 0.25}) == {
        "type": "set_params",
        "seq": 8,
        "params": {"base_speed": 0.25},
    }
    assert build_action(seq=9, action_id="a-0001", name="move_cell", speed=0.25, target_ticks=1350) == {
        "type": "action",
        "seq": 9,
        "action_id": "a-0001",
        "name": "move_cell",
        "speed": 0.25,
        "target_ticks": 1350,
    }
```

- [ ] **Step 2: Run protocol tests to verify they fail**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_protocol.py -q`

Expected: FAIL because `rdk_maze_tuner.core.protocol` is not implemented yet.

- [ ] **Step 3: Implement protocol module**

Create a compact JSON encoder, newline decoder, typed validation helpers, and message builders exactly matching `AGENTS.md`.

- [ ] **Step 4: Run protocol tests to verify they pass**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_protocol.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing parameter tests**

```python
from pathlib import Path

from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError


def test_param_manager_loads_nested_values_and_flattens_esp32_params():
    manager = ParamManager(
        params_path=Path("rdk_maze_tuner/config/params.yaml"),
        limits_path=Path("rdk_maze_tuner/config/limits.yaml"),
    )
    assert manager.get("motor.base_speed") == 0.25
    assert manager.esp32_params()["base_speed"] == 0.25
    assert manager.esp32_params()["cell_ticks"] == 1350


def test_param_manager_rejects_out_of_range_update():
    manager = ParamManager(
        params_path=Path("rdk_maze_tuner/config/params.yaml"),
        limits_path=Path("rdk_maze_tuner/config/limits.yaml"),
    )
    try:
        manager.apply_updates({"motor.base_speed": 2.0}, source="test")
    except ParamValidationError as exc:
        assert "motor.base_speed" in str(exc)
    else:
        raise AssertionError("expected ParamValidationError")
```

- [ ] **Step 6: Run parameter tests to verify they fail**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_param_manager.py -q`

Expected: FAIL because `ParamManager` is not implemented yet.

- [ ] **Step 7: Implement parameter manager and config files**

Implement dotted-path get/update, range validation, parameter versioning, and ESP32 flattened parameter export.

- [ ] **Step 8: Run parameter tests to verify they pass**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_param_manager.py -q`

Expected: PASS.

### Task 2: RDK Maze Core

**Files:**
- Create: `rdk_maze_tuner/core/maze_map.py`
- Create: `rdk_maze_tuner/core/maze_planner.py`
- Test: `rdk_maze_tuner/tests/test_maze_core.py`

- [ ] **Step 1: Write failing maze tests**

```python
from rdk_maze_tuner.core.maze_map import Direction, MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner


def test_maze_map_converts_local_walls_to_global_walls():
    maze = MazeMap(wall_threshold_mm=150)
    maze.observe(front_mm=100, left_mm=300, right_mm=90)
    cell = maze.cell((0, 0))
    assert cell.walls["N"] is True
    assert cell.walls["W"] is False
    assert cell.walls["E"] is True


def test_planner_waits_for_done_before_advancing_position():
    maze = MazeMap(wall_threshold_mm=150)
    planner = MazePlanner()
    maze.observe(front_mm=300, left_mm=90, right_mm=90)
    action = planner.next_action(maze)
    assert action.name == "move_cell"
    assert maze.position == (0, 0)
    maze.apply_completed_action(action)
    assert maze.position == (0, 1)
    assert maze.heading == Direction.NORTH
```

- [ ] **Step 2: Run maze tests to verify they fail**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_maze_core.py -q`

Expected: FAIL because maze modules are not implemented yet.

- [ ] **Step 3: Implement maze map and planner**

Implement direction enum, wall observations, neighbor linking, action planning, and action completion separate from command creation.

- [ ] **Step 4: Run maze tests to verify they pass**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_maze_core.py -q`

Expected: PASS.

### Task 3: ESP32 Firmware Skeleton

**Files:**
- Create: `esp32_firmware/platformio.ini`
- Create: `esp32_firmware/include/config.h`
- Create: `esp32_firmware/include/params.h`
- Create: `esp32_firmware/include/protocol.h`
- Create: `esp32_firmware/include/motor.h`
- Create: `esp32_firmware/include/encoder.h`
- Create: `esp32_firmware/include/tof_sensors.h`
- Create: `esp32_firmware/include/motion_controller.h`
- Create: `esp32_firmware/include/safety.h`
- Create: `esp32_firmware/src/main.cpp`
- Create: `esp32_firmware/src/params.cpp`
- Create: `esp32_firmware/src/protocol.cpp`
- Create: `esp32_firmware/src/motor.cpp`
- Create: `esp32_firmware/src/encoder.cpp`
- Create: `esp32_firmware/src/tof_sensors.cpp`
- Create: `esp32_firmware/src/motion_controller.cpp`
- Create: `esp32_firmware/src/safety.cpp`

- [ ] **Step 1: Create PlatformIO project structure**

Use `esp32dev`, Arduino framework, `ArduinoJson`, and `pololu/VL53L0X`.

- [ ] **Step 2: Implement JSON protocol entry points**

Implement `handleSerialLine`, `sendReady`, `sendTelemetry`, `sendDone`, `sendError`, and `sendAck` using the documented newline JSON contract.

- [ ] **Step 3: Implement non-blocking motion skeleton**

Implement `MotionController::start`, `MotionController::tick`, `MotionController::stop`, and `MotionController::estop` without long blocking loops.

- [ ] **Step 4: Preserve pin facts**

Keep motor, encoder, and VL53 XSHUT pin values from `AGENTS.md`.

### Task 4: Verification

**Files:**
- Existing and newly created files.

- [ ] **Step 1: Run Python tests**

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Expected: PASS.

- [ ] **Step 2: Run Python compile check**

Run: `python3 -m compileall rdk_maze_tuner`

Expected: PASS.

- [ ] **Step 3: Check ESP32 build tool availability**

Run: `command -v pio`

Expected: either a path to PlatformIO or report that PlatformIO is not installed.

- [ ] **Step 4: If PlatformIO is available, compile firmware**

Run: `cd esp32_firmware && pio run`

Expected: PASS. If PlatformIO is unavailable, report that firmware compile is pending tool installation.

### Task 5: RDK Serial Runtime Layer

**Files:**
- Create: `rdk_maze_tuner/core/serial_client.py`
- Create: `rdk_maze_tuner/main.py`
- Create: `rdk_maze_tuner/tests/test_serial_client.py`
- Create: `rdk_maze_tuner/tests/test_main_cli.py`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.gitignore`

- [x] **Step 1: Write fake-serial tests for ready, ack, done, error, and timeout**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_serial_client.py -q`

Observed RED first: `ModuleNotFoundError: No module named 'rdk_maze_tuner.core.serial_client'`

- [x] **Step 2: Implement `SerialClient`**

Implemented synchronous newline JSON client with sequence ids, ack matching, telemetry tracking, action result waiting, stop, estop, and pyserial-backed `open_serial()`.

- [x] **Step 3: Add command-line RDK entry point**

Implemented `python3 rdk_maze_tuner/main.py --serial /dev/ttyUSB0 --baud 115200`, with ready wait, parameter upload, and optional one-shot action.

- [x] **Step 4: Add direct-script CLI regression test**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_main_cli.py -q`

Observed RED first: `ImportError: attempted relative import with no known parent package`

- [x] **Step 5: Fix direct script imports**

`main.py` now inserts the project root into `sys.path` only when run as a direct script, then uses absolute package imports.

- [x] **Step 6: Verify runtime layer**

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Expected: PASS.

Run: `python3 rdk_maze_tuner/main.py --help`

Expected: command help prints successfully.

### Task 6: RDK Automatic Exploration Loop

**Files:**
- Create: `rdk_maze_tuner/core/maze_runner.py`
- Create: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `rdk_maze_tuner/core/serial_client.py`
- Modify: `rdk_maze_tuner/core/maze_map.py`
- Modify: `rdk_maze_tuner/main.py`
- Modify: `rdk_maze_tuner/tests/test_main_cli.py`

- [x] **Step 1: Write fake-ESP32 exploration tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_maze_runner.py -q`

Observed RED first: `ModuleNotFoundError: No module named 'rdk_maze_tuner.core.maze_runner'`

- [x] **Step 2: Implement telemetry wait**

`SerialClient.wait_telemetry()` now waits for a `telemetry` message while preserving `last_telemetry`.

- [x] **Step 3: Implement `MazeRunner`**

`MazeRunner.run_step()` now performs one RDK exploration cycle: wait for telemetry, observe walls, ask planner for next action, send action-level command, wait for `done/error`, update map only after done, and return a step result with map text.

- [x] **Step 4: Add ASCII map rendering**

`MazeMap.render_ascii()` now renders current position, heading, visited cells, known walls, open paths, and unknown walls.

- [x] **Step 5: Add command-line explore mode**

`main.py` now supports `--mode setup`, `--mode action`, and `--mode explore --steps N`.

- [x] **Step 6: Verify exploration loop**

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Expected: PASS.

Run: `python3 rdk_maze_tuner/main.py --help`

Expected: help includes `--mode {setup,action,explore}` and `--steps`.

### Task 7: Motion Analysis And Rule-Based Auto Tuning

**Files:**
- Create: `rdk_maze_tuner/core/motion_analyzer.py`
- Create: `rdk_maze_tuner/core/auto_tuner.py`
- Create: `rdk_maze_tuner/tests/test_motion_analyzer.py`
- Create: `rdk_maze_tuner/tests/test_auto_tuner.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/main.py`
- Modify: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `rdk_maze_tuner/tests/test_main_cli.py`

- [x] **Step 1: Write motion analyzer and auto tuner tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_motion_analyzer.py rdk_maze_tuner/tests/test_auto_tuner.py -q`

Observed RED first: missing `rdk_maze_tuner.core.motion_analyzer` and `rdk_maze_tuner.core.auto_tuner`.

- [x] **Step 2: Implement `MotionAnalyzer`**

Analyzer now converts action `done/error` payloads into `MotionReport`, including encoder delta, average ticks, distance error, left/right ratio, issue labels, and confidence.

- [x] **Step 3: Implement `AutoTuner`**

Auto tuner now applies documented rules for drift, move short/long, turn over/under, and obstacle-too-close cases through `ParamManager.apply_updates()`, respecting `limits.yaml`, `auto_tune.max_params_per_step`, and never modifying safety parameters.

- [x] **Step 4: Integrate optional analyzer/tuner into `MazeRunner`**

`MazeRunner.run_step()` now returns optional `motion_report` and `tune_event` after successful action completion.

- [x] **Step 5: Add explore CLI auto-tune switch**

`main.py --mode explore` now enables rule-based auto tuning by default and supports `--no-auto-tune` for observation-only real-car testing.

- [x] **Step 6: Verify M4 first layer**

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Expected: PASS.

Run: `python3 -m compileall rdk_maze_tuner`

Expected: PASS.

Run: `cd esp32_firmware && python3 -m platformio run`

Expected: PASS.

### Task 8: Experiment Logging And Export

**Files:**
- Create: `rdk_maze_tuner/core/logger.py`
- Create: `rdk_maze_tuner/tests/test_logger_exports.py`
- Modify: `rdk_maze_tuner/core/maze_map.py`
- Modify: `rdk_maze_tuner/core/param_manager.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/main.py`
- Modify: `rdk_maze_tuner/tests/test_main_cli.py`

- [x] **Step 1: Write JSONL and export tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_logger_exports.py -q`

Observed RED first: missing `rdk_maze_tuner.core.logger`.

- [x] **Step 2: Implement `JsonlLogger`**

Logger now writes one compact JSON object per line with `ts_ms`, `type`, and JSON-ready `payload`, including dataclass, enum, tuple/list, and path conversion.

- [x] **Step 3: Implement map and parameter export**

`MazeMap.to_dict()` now exports position, start, heading, visited cells, known walls, and ASCII map. `ParamManager.snapshot()` exports full params plus ESP32-flattened params.

- [x] **Step 4: Integrate logging into `MazeRunner`**

Runner now records `telemetry`, `planned_action`, `done`, `motion_report`, `param_change`, and `maze_update` when a logger is provided.

- [x] **Step 5: Add CLI logging/export options**

`main.py` now supports `--log-file`, `--export-map`, and `--export-params`.

- [x] **Step 6: Verify logging/export layer**

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Expected: PASS.

Run: `python3 -m compileall rdk_maze_tuner`

Expected: PASS.

Run: `cd esp32_firmware && python3 -m platformio run`

Expected: PASS.

### Task 9: FastAPI Dashboard First Version

**Files:**
- Create: `rdk_maze_tuner/dashboard/__init__.py`
- Create: `rdk_maze_tuner/dashboard/state.py`
- Create: `rdk_maze_tuner/dashboard/app.py`
- Create: `rdk_maze_tuner/dashboard/templates/index.html`
- Create: `rdk_maze_tuner/dashboard/static/app.css`
- Create: `rdk_maze_tuner/dashboard/static/app.js`
- Create: `rdk_maze_tuner/tests/test_dashboard.py`
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`

- [x] **Step 1: Write failing Dashboard API and WebSocket tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed RED first: `ModuleNotFoundError: No module named 'rdk_maze_tuner.dashboard'`

- [x] **Step 2: Implement Dashboard state container**

`DashboardState` now exposes offline-safe snapshots, parameter updates through `ParamManager`, command logging, auto-tune toggling, and `estop` / `stop` command paths that can run with or without a real `SerialClient`.

- [x] **Step 3: Implement FastAPI app**

`create_app()` now serves `/`, `/api/state`, `/api/params`, `/api/command/estop`, `/api/command/stop`, `/api/command/action`, `/api/auto-tune`, and `/ws`.

- [x] **Step 4: Implement single-page tuning workspace**

The first screen now shows connection state, ESP32 state, always-visible emergency stop, maze map, telemetry metrics, manual controls, editable parameters, auto-tune status, and event logs.

- [x] **Step 5: Add Dashboard dependencies**

`requirements.txt` now includes FastAPI and Uvicorn. `requirements-dev.txt` now includes HTTPX for FastAPI `TestClient`.

- [x] **Step 6: Verify Dashboard layer**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed: `6 passed in 0.28s`

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Observed: `36 passed in 0.47s`

Run: `python3 -m compileall rdk_maze_tuner`

Observed: compile completed successfully.

Run: `python3 rdk_maze_tuner/dashboard/app.py --help`

Observed: CLI help prints `RDK X3 maze tuning dashboard` and host/port/serial options.

Run: `cd esp32_firmware && python3 -m platformio run`

Observed: firmware build `SUCCESS`, RAM 6.9%, Flash 24.1%.

Run: `curl --noproxy '*' -sS http://127.0.0.1:8000/api/state`

Observed: offline-safe state returns parameters, maze snapshot, telemetry defaults, and logs.

Browser note: in-app Browser navigation to `http://127.0.0.1:8000/` was blocked by Browser Use URL policy, so visual browser verification was not completed in that tool. HTTP-level page and asset checks confirmed the dashboard HTML, CSS, and JS are served.

### Task 10: Dashboard Realtime Serial Bridge

**Files:**
- Create: `rdk_maze_tuner/dashboard/runtime.py`
- Modify: `rdk_maze_tuner/dashboard/state.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/dashboard/static/app.js`
- Modify: `rdk_maze_tuner/tests/test_dashboard.py`

- [x] **Step 1: Write failing realtime runtime tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed RED first: `ModuleNotFoundError: No module named 'rdk_maze_tuner.dashboard.runtime'`

- [x] **Step 2: Implement `SerialDashboardRuntime`**

Runtime now has `poll_once()` for fake/real serial message ingestion, `send_heartbeat_once()` for ESP32 heartbeat ack, and async startup/shutdown loops for FastAPI lifespan.

- [x] **Step 3: Add serial message handling to Dashboard state**

`DashboardState` now converts incoming `telemetry`, `ready`, `ack`, `done`, and `error` frames into dashboard state and bounded logs. Serial reads, heartbeat, parameter updates, stop, and estop share the same state lock so background telemetry polling does not steal command acks.

- [x] **Step 4: Wire runtime into FastAPI lifespan**

`create_app()` now attaches `app.state.runtime` and starts/stops the runtime automatically when a serial client is supplied.

- [x] **Step 5: Add WebSocket realtime refresh from the frontend**

The single-page UI now sends a WebSocket `ping` every 300ms and renders the returned `pong` snapshot, while keeping HTTP polling as a fallback.

- [x] **Step 6: Verify realtime bridge**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed: `9 passed in 0.35s`

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Observed: `39 passed in 0.40s`

Run: `python3 -m compileall rdk_maze_tuner`

Observed: compile completed successfully.

Run: `python3 rdk_maze_tuner/dashboard/app.py --help`

Observed: CLI help prints dashboard host/port/serial options.

Run: `python3 -m platformio run` in `esp32_firmware/`

Observed: firmware build `SUCCESS`, RAM 6.9%, Flash 24.1%.

Run: `curl --noproxy '*' -sS http://127.0.0.1:8000/api/state`

Observed: offline-safe dashboard state returns parameters, maze, telemetry defaults, and logs.

Run: `curl --noproxy '*' -sS http://127.0.0.1:8000/static/app.js | rg 'socket.send\\(JSON.stringify\\(\\{ type: "ping" \\}\\)\\)|pingTimer|connectSocket'`

Observed: frontend bundle includes WebSocket ping refresh and reconnect timer.

### Task 11: Dashboard Manual Action Closed Loop

**Files:**
- Modify: `rdk_maze_tuner/core/serial_client.py`
- Modify: `rdk_maze_tuner/dashboard/state.py`
- Modify: `rdk_maze_tuner/dashboard/static/app.js`
- Modify: `rdk_maze_tuner/tests/test_dashboard.py`

- [x] **Step 1: Write failing manual action closed-loop tests**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed RED first: 3 failures because dashboard manual actions were only local log records: `sent_to_esp32` stayed `False`, no `action_id` was returned, and action errors did not become dashboard `error` results.

- [x] **Step 2: Expose ack + result action execution in `SerialClient`**

`SerialClient.execute_action_with_ack()` now sends one documented action message, waits for matching ack, then returns the matching `done` or `error` frame. Existing `execute_action()` keeps its previous behavior and raises on action error for CLI/explore callers.

- [x] **Step 3: Implement manual action dispatch in Dashboard state**

`DashboardState.manual_action()` now generates `dashboard-0001` style action ids, resolves action speed/ticks from `ParamManager`, sends the action to ESP32 when connected, records `planned_action`, `ack`, and `done/error`, and updates the map only after successful `done`.

- [x] **Step 4: Improve frontend current-action display**

The Dashboard header now displays action name, action id, result type, and error code when present.

- [x] **Step 5: Verify manual action closed loop**

Run: `python3 -m pytest rdk_maze_tuner/tests/test_dashboard.py -q`

Observed: `12 passed in 0.28s`

Run: `python3 -m pytest rdk_maze_tuner/tests -q`

Observed: `42 passed in 0.41s`

Run: `python3 -m compileall rdk_maze_tuner`

Observed: compile completed successfully.

Run: `python3 rdk_maze_tuner/dashboard/app.py --help`

Observed: CLI help prints dashboard host/port/serial options.

Run: `python3 -m platformio run` in `esp32_firmware/`

Observed: firmware build `SUCCESS`, RAM 6.9%, Flash 24.1%.
