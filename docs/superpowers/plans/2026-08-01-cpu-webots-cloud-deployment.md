# CPU Webots Cloud Deployment Implementation Plan

**Goal:** Securely bootstrap the purchased Ubuntu 24.04 CPU server, deploy ROS2 Jazzy and Webots R2025a, add a CPU-light Webots maze simulation adapter, and expose Dashboard/Webots/noVNC only through SSH tunnels.

**Architecture:** The existing RDK control stack remains the source of truth. A TCP newline-JSON transport mirrors the real ESP32 serial protocol. A Webots controller acts as the simulated ESP32 and returns ready/ack/telemetry/done/error. Local Mac remains responsible for PlatformIO builds and physical USB flashing.

**Safety:** Never persist the bootstrap password. Verify key login before disabling password authentication. Do not expose internal simulation ports publicly. Do not flash hardware in this plan.

## Task 1: Secure SSH Bootstrap

- [ ] Create a dedicated local Ed25519 key without overwriting existing keys.
- [ ] Install its public key using the one-time bootstrap password.
- [ ] Verify non-interactive key login.
- [ ] Inspect OS, CPU, RAM, disk, network listeners, cloud-init and failed services.
- [ ] Create an unprivileged `maze` runtime account and install the public key.
- [ ] Verify `maze` key login.
- [ ] Install SSH hardening configuration and validate with `sshd -t`.
- [ ] Disable password and root SSH login only after key verification.

## Task 2: Host Baseline

- [ ] Update Ubuntu packages.
- [ ] Install build tools, Python venv tooling, rsync, jq, tmux, UFW and Fail2ban.
- [ ] Set timezone to Asia/Shanghai.
- [ ] Configure UFW for SSH only; internal services bind to loopback.
- [ ] Create `/srv/maze/releases`, `/srv/maze/shared/logs` and `/srv/maze/shared/exports`.

## Task 3: TCP Simulation Transport

- [ ] Add a socket-backed line transport compatible with `SerialClient`.
- [ ] Add CLI support for mutually exclusive `--serial` and `--tcp`.
- [ ] Add dashboard support for the same transport selection.
- [ ] Add fake TCP tests for newline framing, timeout, reconnect and action correlation.
- [ ] Preserve the existing serial path and safety contract.

## Task 4: Webots Maze Simulator

- [ ] Add a CPU-light Webots world with floor, maze walls and a visible differential-drive car.
- [ ] Add a Python Webots controller that implements the documented JSON protocol.
- [ ] Simulate front/left/right distance, encoders, action timing and estop.
- [ ] Use action-level motion and deterministic telemetry; do not claim motor-physics equivalence.
- [ ] Provide Webots stream, headless and automated-test launch commands.
- [ ] Add protocol-level tests that do not require Webots.

## Task 5: Deployment Artifacts

- [ ] Add redacted environment templates.
- [ ] Add systemd units for Dashboard, Webots stream, headless simulation and noVNC.
- [ ] Add an idempotent server bootstrap script.
- [ ] Add a versioned release/upload script that never uses destructive sync.
- [ ] Add a Mac SSH-tunnel helper that reads the server host from the user SSH config rather than storing it in the project.
- [ ] Document start, stop, logs, rollback and collaborator access.

## Task 6: Local Verification

- [ ] Create an isolated local Python environment with `uv`.
- [ ] Run all Python tests.
- [ ] Run Python compile checks.
- [ ] Install PlatformIO into the isolated environment if needed and compile firmware.
- [ ] Do not upload firmware without a fresh hardware confirmation.

## Task 7: Server Software

- [ ] Upload the verified project snapshot to a new release directory.
- [ ] Install ROS2 Jazzy from the official repository.
- [ ] Verify ROS2 talker/listener.
- [ ] Install Webots R2025a from the official release.
- [ ] Install Xvfb, Mesa software OpenGL, Xfce, TigerVNC and noVNC.
- [ ] Verify `webots --sysinfo` and an official minimal headless world.

## Task 8: Server Runtime Verification

- [ ] Install project dependencies in `/srv/maze/venv`.
- [ ] Run project tests and compile checks on the server.
- [ ] Start the Webots maze controller and TCP transport.
- [ ] Verify ready/ack/telemetry/done/error correlation.
- [ ] Start Dashboard against the TCP simulator.
- [ ] Verify services listen only on loopback.
- [ ] Verify Webots browser streaming through an SSH tunnel.
- [ ] Verify noVNC through an SSH tunnel; record CPU-only GUI limitations accurately.

## Task 9: Handoff

- [ ] Record installed versions and service status without credentials.
- [ ] Provide the SSH tunnel command and local URLs.
- [ ] Ask the user to rotate the exposed bootstrap password after key-login acceptance.
- [ ] Leave physical RDK X3 and ESP32 validation as a separate explicit acceptance stage.
