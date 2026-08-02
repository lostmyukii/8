let parameterFingerprint = "";
let noticeTimer = null;

const $ = (id) => document.getElementById(id);

const HEADING = {
  N: { degrees: 0, label: "北" },
  E: { degrees: 90, label: "东" },
  S: { degrees: 180, label: "南" },
  W: { degrees: 270, label: "西" },
};

const TASK_ORDER = [
  "PREFLIGHT",
  "READY",
  "RUNNING",
  "FINALIZING",
  "COMPLETED",
];

const UNITS = {
  speed: "比例",
  ticks: "ticks",
  pwm: "PWM",
  mm: "mm",
  cm: "cm",
  kp: "gain",
  ki: "gain",
  kd: "gain",
  timeout: "ms",
  window: "samples",
  trim: "ratio",
  correction: "ratio",
};

function setText(id, value, fallback = "-") {
  const element = $(id);
  if (!element) return;
  element.textContent =
    value === null || value === undefined || value === "" ? fallback : String(value);
}

function setLamp(id, online) {
  $(id)?.classList.toggle("is-online", Boolean(online));
}

function valueText(value, fallback = "-") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function numericText(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "-";
}

function flattenParams(node, prefix = "", rows = []) {
  Object.entries(node || {}).forEach(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      flattenParams(value, path, rows);
    } else {
      rows.push([path, value]);
    }
  });
  return rows;
}

function unitFor(path) {
  const part = path.split(".").at(-1) || "";
  const matched = Object.entries(UNITS).find(([key]) => part.includes(key));
  return matched?.[1] || (typeof part === "boolean" ? "bool" : "-");
}

function actionText(action) {
  if (!action) return "待机";
  return [action.name || action.type, action.action_id, action.code]
    .filter(Boolean)
    .join(" · ");
}

function activeTaskFrom(appState) {
  return appState.activeTask || null;
}

export function renderAuthGate(authenticated) {
  $("loginGate")?.classList.toggle("is-hidden", Boolean(authenticated));
  $("appShell")?.setAttribute("aria-hidden", authenticated ? "false" : "true");
}

export function showNotice(message, { error = false, timeout = 3200 } = {}) {
  const notice = $("globalNotice");
  if (!notice) return;
  notice.textContent = message;
  notice.classList.toggle("is-error", error);
  notice.classList.add("is-visible");
  if (noticeTimer) window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => {
    notice.classList.remove("is-visible");
  }, timeout);
}

export function renderDashboard(appState) {
  renderAuthGate(appState.authenticated);
  const payload = appState.payload;
  if (!payload) return;

  const telemetry = payload.telemetry || {};
  const maze = payload.maze || {};
  const params = payload.params || {};
  const auth = payload.auth || {};
  const control = auth.control || {};
  const task = activeTaskFrom(appState);
  const selectedMode = task?.mode || appState.selectedMode;
  const isController =
    control.role === "controller" && Boolean(appState.leaseToken);

  setText("userName", auth.user?.username, "-");
  renderLease(control, isController);
  renderMode(selectedMode, task, isController);
  renderConnections(appState, selectedMode, task, telemetry);
  renderTask(task, isController, appState);
  renderPhysicalProfileSelector(appState, task, isController);
  renderPose(maze, telemetry, params, task);
  renderEvidence(telemetry);
  renderPhysicalEvidence(
    payload.physical_evidence || {},
    selectedMode,
  );
  renderMap(maze);
  renderTimeline(payload.logs || [], task?.recent_events || []);
  renderTune(payload.logs || []);
  renderParams(params, isController);

  $("autoTuneToggle").checked = Boolean(payload.auto_tune_enabled);
  $("autoTuneToggle").disabled = !isController;
  setText("paramVersion", `参数 v${valueText(params.param_version, "1")}`);
  setText("currentAction", actionText(payload.current_action));
  setText(
    "lastAck",
    payload.last_ack ? `ACK ${valueText(payload.last_ack.seq)}` : "ACK -",
  );
}

function renderPhysicalProfileSelector(appState, task, isController) {
  const select = $("physicalProfileInput");
  if (!select) return;
  const profiles = appState.physicalProfiles || [];
  const options = profiles.map((profile) => {
    const option = document.createElement("option");
    option.value = profile.profile_id;
    option.textContent = `${profile.profile_id} · ${
      String(profile.digest || "").slice(0, 10)
    }`;
    return option;
  });
  if (!options.length) {
    const option = document.createElement("option");
    option.value = appState.selectedPhysicalProfileId || "normal-v1";
    option.textContent = option.value;
    options.push(option);
  }
  select.replaceChildren(...options);
  const taskLocksProfile = Boolean(
    task
    && task.mode === "simulation"
    && !["IDLE", "COMPLETED", "LOST", "ERROR", "ESTOP"].includes(
      task.status,
    ),
  );
  select.value =
    taskLocksProfile && task.physical_profile_id
      ? task.physical_profile_id
      : appState.selectedPhysicalProfileId;
  select.disabled =
    appState.selectedMode !== "simulation"
    || !isController
    || taskLocksProfile;
  select.title = taskLocksProfile
    ? "物理配置已经随本次任务锁定；完成后重置可更换"
    : "配置在任务创建时形成不可变快照";
}

function joinPair(left, right, unit = "") {
  return `${numericText(left)}${unit} / ${numericText(right)}${unit}`;
}

function percentagePair(left, right) {
  const format = (value) => (
    Number.isFinite(Number(value))
      ? `${numericText(Number(value) * 100)}%`
      : "-"
  );
  return `${format(left)} / ${format(right)}`;
}

function poseText(pose) {
  return `x ${numericText(pose?.x_mm)} · y ${numericText(
    pose?.y_mm,
  )} mm · ${numericText(pose?.yaw_deg)}°`;
}

function renderPhysicalEvidence(evidence, selectedMode) {
  const profile = evidence.profile || {};
  const vehicle = evidence.vehicle || {};
  const wheel = evidence.wheel || {};
  const tof = evidence.tof || {};
  const imu = evidence.imu || {};
  const control = evidence.control || {};
  const slip = evidence.slip || {};
  const pose = evidence.pose || {};
  const safety = evidence.safety || {};
  const isSimulation = selectedMode === "simulation";
  const truth_evaluation_only =
    isSimulation && pose.truth_evaluation_only === true && pose.truth;

  const mode = $("physicalEvidenceMode");
  if (mode) {
    mode.textContent = profile.profile_id
      ? isSimulation
        ? "SIM PHYSICS"
        : "REAL SENSORS"
      : "等待物理遥测";
    mode.className = `status-pill ${
      profile.profile_id || control.state
        ? "status-pill--online"
        : "status-pill--lost"
    }`;
  }

  setText("physicalProfileId", profile.profile_id);
  setText(
    "physicalProfileDigest",
    profile.digest ? String(profile.digest).slice(0, 16) : null,
  );
  setText("physicalRandomSeed", profile.random_seed);
  setText("physicalWebotsVersion", isSimulation ? profile.webots_version : "实车");
  setText(
    "physicalMass",
    vehicle.total_mass_kg !== null
      && vehicle.total_mass_kg !== undefined
      && Number.isFinite(Number(vehicle.total_mass_kg))
      ? `${numericText(vehicle.total_mass_kg, 3)} kg`
      : null,
  );
  setText(
    "physicalCenterOfMass",
    Array.isArray(vehicle.center_of_mass_m)
      ? vehicle.center_of_mass_m.map((value) => numericText(value, 3)).join(" / ")
      : null,
  );
  setText(
    "physicalWheelGeometry",
    vehicle.wheel_radius_m !== null
      && vehicle.wheel_radius_m !== undefined
      && Number.isFinite(Number(vehicle.wheel_radius_m))
      ? `R ${numericText(Number(vehicle.wheel_radius_m) * 1000)} / T ${
        numericText(Number(vehicle.axle_track_m) * 1000)
      } mm`
      : null,
  );
  setText("physicalSurface", vehicle.surface_profile);

  setText(
    "wheelEvidence",
    `角 ${joinPair(
      wheel.wheel_angle_left_rad,
      wheel.wheel_angle_right_rad,
      " rad",
    )} · 速 ${joinPair(
      wheel.wheel_speed_left_rad_s,
      wheel.wheel_speed_right_rad_s,
      " rad/s",
    )} · PWM ${joinPair(wheel.pwm_left, wheel.pwm_right)} · 力矩 ${
      joinPair(
        wheel.motor_torque_left_nm,
        wheel.motor_torque_right_nm,
        " Nm",
      )
    } · 编码 ${joinPair(wheel.enc_left, wheel.enc_right)}`,
  );
  const quality = Array.isArray(tof.quality_flags)
    ? tof.quality_flags.join(", ")
    : valueText(tof.quality_flags, "-");
  setText(
    "tofEvidence",
    `原始 F/L/R ${numericText(tof.raw_front_mm)} / ${
      numericText(tof.raw_left_mm)
    } / ${numericText(tof.raw_right_mm)} mm · 滤波 ${
      numericText(tof.front_mm)
    } / ${numericText(tof.left_mm)} / ${
      numericText(tof.right_mm)
    } mm · ${quality}`,
  );
  setText(
    "imuEvidence",
    imu.imu_available
      ? `yaw ${numericText(imu.imu_yaw_deg)}° · ${
        numericText(imu.yaw_rate_dps)
      }°/s · a ${numericText(imu.accel_forward_mps2)} m/s² · 置信 ${
        numericText(Number(imu.pose_confidence) * 100, 0)
      }%`
      : "未配置 / 降级到编码器与墙面约束",
  );
  setText(
    "controlEvidence",
    `${valueText(control.state, "IDLE")} · ${
      valueText(control.action_id, "无 action_id")
    } · 进度 ${numericText(control.progress_ticks)} ticks · 剩余 ${
      numericText(control.remaining_ticks)
    } ticks · 航差 ${numericText(control.heading_error_deg)}° · tick ${
      numericText(control.controller_period_ms)
    } ms`,
  );
  setText(
    "slipEstimateEvidence",
    `${percentagePair(
      slip.estimated_left,
      slip.estimated_right,
    )} · ${valueText(slip.estimated_quality, "insufficient")}`,
  );
  setText(
    "estimatedPoseEvidence",
    `${poseText(pose.estimated)} · 置信 ${
      Number.isFinite(Number(pose.estimated?.confidence))
        ? `${numericText(Number(pose.estimated.confidence) * 100, 0)}%`
        : "-"
    }`,
  );
  setText(
    "actionCompletionEvidence",
    control.action_id
      ? `${control.action_id} · ${valueText(control.state)} · 剩余 ${
        numericText(control.remaining_ticks)
      } ticks`
      : "等待动作",
  );
  setText(
    "safetyEvidence",
    `${valueText(safety.state, "OFFLINE")} · ${
      Array.isArray(safety.quality_flags) && safety.quality_flags.length
        ? safety.quality_flags.join(", ")
        : "无安全告警"
    }`,
  );
  setText(
    "lastPhysicalError",
    safety.last_error
      ? `${valueText(safety.last_error.code, "ERROR")} · ${
        valueText(safety.last_error.message, safety.last_error.reason)
      }`
      : "无",
  );

  const truthCard = $("truthEvaluationCard");
  truthCard.hidden = !truth_evaluation_only;
  if (truth_evaluation_only) {
    setText(
      "poseComparisonEvidence",
      `估值 ${poseText(pose.estimated)} · 真值 ${
        poseText(pose.truth)
      } · 位置误差 ${numericText(pose.position_error_mm)} mm · 航向误差 ${
        numericText(pose.yaw_error_deg)
      }°`,
    );
    setText(
      "truthSlipEvidence",
      `真值 L/R ${
        percentagePair(slip.truth_left, slip.truth_right)
      } · 估值差 ${
        percentagePair(slip.left_delta, slip.right_delta)
      } · 仅评估，不进入控制闭环`,
    );
  }
}

function renderLease(control, isController) {
  const holder = control.holder;
  const role = control.role || "viewer";
  setText(
    "leaseState",
    isController
      ? "你正在控制"
      : holder
        ? `${holder.username} 正在控制`
        : "只读观看",
  );
  setText(
    "leaseTimer",
    holder
      ? `${valueText(control.remaining_seconds, 0)} 秒后续租`
      : "控制权当前空闲",
  );
  $("claimControlButton").hidden = isController;
  $("claimControlButton").disabled = Boolean(holder) && role !== "controller";
  $("releaseControlButton").hidden = !isController;
  setText(
    "controlHint",
    isController
      ? "你持有控制权。所有任务操作会写入审计记录。"
      : holder
        ? `当前由 ${holder.username} 操作；你可以观看并随时急停。`
        : "当前为只读观看。取得控制权后可操作任务。",
  );
}

function renderMode(selectedMode, task, isController) {
  const switchLocked = Boolean(task && !task.can_switch_mode);
  ["simulation", "real"].forEach((mode) => {
    const button = $(mode === "simulation" ? "modeSimulation" : "modeReal");
    button.classList.toggle("is-active", selectedMode === mode);
    button.disabled = switchLocked || !isController;
    button.setAttribute("aria-pressed", selectedMode === mode ? "true" : "false");
  });
  $("streamFrame").dataset.mode = selectedMode;
  setText(
    "streamModeLabel",
    selectedMode === "simulation" ? "SIM / WEBOTS" : "REAL / RDK CAMERA",
  );
  setText(
    "streamTitle",
    selectedMode === "simulation" ? "等待 Webots 画面" : "等待 RDK 相机画面",
  );
  setText(
    "streamHint",
    selectedMode === "simulation"
      ? "仿真服务连接后在这里显示实时视频"
      : "RDK Agent 上线后通过鉴权通道显示画面",
  );
}

function renderConnections(appState, selectedMode, task, telemetry) {
  const socketOnline = appState.socketConnected;
  const deviceOnline = Boolean(appState.payload?.connected);
  const adapterOnline = Boolean(task?.adapter?.connected);
  const webotsOnline =
    selectedMode === "simulation" && (adapterOnline || deviceOnline);
  const rdkOnline = selectedMode === "real" && adapterOnline;

  setLamp("webotsLamp", webotsOnline);
  setLamp("rdkLamp", rdkOnline);
  setLamp("esp32Lamp", deviceOnline);
  setText("webotsState", webotsOnline ? "在线" : "待机");
  setText("rdkState", rdkOnline ? "在线" : "离线");
  setText("esp32State", telemetry.state || "OFFLINE");

  const connectionState = $("connectionState");
  connectionState.textContent = socketOnline ? "ONLINE" : "LOST";
  connectionState.className = `status-pill ${
    socketOnline ? "status-pill--online" : "status-pill--lost"
  }`;
}

function renderTask(task, isController, appState) {
  const status = task?.status || "IDLE";
  setText("taskState", status);
  $("taskState").dataset.state = status;
  setText("taskId", task?.task_id || "尚未创建");
  setText("runId", task?.run_id || task?.last_run_id || "-");
  setText("stepCount", `${task?.step_count || 0} / ${task?.max_steps || 500}`);
  setText("lastStepOutcome", task?.last_step?.outcome || "-");

  const blockedPreflight =
    status === "PREFLIGHT" && task?.preflight?.ok === false;
  const mapGoalReady = (
    appState.mapVersionStatus === "ready"
    && Boolean(appState.mapGoal)
    && appState.mapVersionDetail?.version_id
      === appState.selectedMapVersionId
  );
  const resetAllowed = !task || blockedPreflight || [
    "IDLE",
    "COMPLETED",
    "LOST",
    "ERROR",
    "ESTOP",
  ].includes(status);
  $("taskResetButton").disabled =
    !isController || !resetAllowed || !mapGoalReady;
  $("taskStartButton").disabled =
    !isController
    || !mapGoalReady
    || !["READY", "PAUSED"].includes(status);
  $("taskPauseButton").disabled = !isController || status !== "RUNNING";
  $("stopButton").disabled =
    !isController ||
    !["READY", "RUNNING", "PAUSING", "PAUSED"].includes(status);

  const manualBlocked =
    !isController ||
    ["RUNNING", "PAUSING", "PAUSED", "FINALIZING"].includes(status);
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = manualBlocked;
  });
  const debugButton = $("debugCoordinateButton");
  if (debugButton) {
    debugButton.disabled = manualBlocked || !mapGoalReady;
  }

  const currentIndex = TASK_ORDER.indexOf(status);
  document.querySelectorAll("[data-task-step]").forEach((step, index) => {
    step.classList.toggle("is-current", step.dataset.taskStep === status);
    step.classList.toggle(
      "is-reached",
      currentIndex >= 0 && index <= currentIndex,
    );
  });
  renderAutomaticGoal(appState);
}

function renderAutomaticGoal(appState) {
  const goal = appState.mapGoal;
  const status = appState.mapVersionStatus;
  const cell = goal?.cell || [];
  const candidates = goal?.candidate_cells || [];
  setText("automaticGoalX", cell[0], "--");
  setText("automaticGoalY", cell[1], "--");
  setText(
    "mapGoalCandidates",
    candidates.length
      ? candidates.map((item) => `(${item[0]},${item[1]})`).join(" / ")
      : "--",
  );
  setText(
    "mapGoalDigest",
    goal?.source_map_digest
      ? String(goal.source_map_digest).slice(0, 12)
      : "--",
  );
  setText(
    "mapGoalPathLength",
    Number.isInteger(goal?.path_length_cells)
      ? `${goal.path_length_cells} 格`
      : "--",
  );
  const statusText = {
    idle: "请选择已保存地图版本",
    loading: "正在校验地图定义、摘要与终点…",
    ready: `已锁定地图终点 (${cell[0]},${cell[1]})`,
    error: appState.mapVersionError || "地图版本或终点加载失败",
  }[status] || "请选择已保存地图版本";
  setText("mapGoalStatus", statusText);
  $("mapGoalStatus")?.classList.toggle("is-error", status === "error");
}

function renderPose(maze, telemetry, params, task) {
  const position = maze.position || [0, 0];
  const heading = HEADING[maze.heading] || HEADING.N;
  const cellSize = Number(params.params?.robot?.cell_size_cm);
  const xCm = Number.isFinite(Number(telemetry.x_cm))
    ? Number(telemetry.x_cm)
    : Number(position[0]) * (Number.isFinite(cellSize) ? cellSize : 25);
  const yCm = Number.isFinite(Number(telemetry.y_cm))
    ? Number(telemetry.y_cm)
    : Number(position[1]) * (Number.isFinite(cellSize) ? cellSize : 25);
  const yaw = Number.isFinite(Number(telemetry.yaw_deg))
    ? Number(telemetry.yaw_deg)
    : heading.degrees;
  const confidence = Number(telemetry.pose_confidence);

  setText("cellPosition", `格 (${position.join(", ")}) · ${maze.heading || "N"}`);
  setText("continuousPose", `x ${numericText(xCm)} cm · y ${numericText(yCm)} cm`);
  setText("headingValue", `${numericText(yaw, 0)}°`);
  setText("headingCardinal", heading.label);
  setText(
    "poseConfidence",
    Number.isFinite(confidence)
      ? `置信度 ${Math.round(confidence * 100)}%`
      : "置信度 未提供",
  );
  setText("poseLabel", `${position.join(",")} ${maze.heading || "N"}`);
  setText("speedValue", `${numericText(telemetry.speed_cm_s)} cm/s`);
  setText(
    "poseCorrection",
    telemetry.pose_correction_reason || "等待定位证据",
  );
  setText(
    "frictionValue",
    Number.isFinite(Number(telemetry.equivalent_friction))
      ? `等效 μ ${numericText(telemetry.equivalent_friction, 2)}`
      : Number.isFinite(Number(telemetry.friction_coefficient))
        ? `μ ${numericText(telemetry.friction_coefficient, 2)}`
        : "μ --",
  );
  setText(
    "slipRate",
    Number.isFinite(Number(telemetry.slip_rate))
      ? `${numericText(Number(telemetry.slip_rate) * 100)}%`
      : "--%",
  );
  setText(
    "truthError",
    Number.isFinite(Number(telemetry.truth_error_cm))
      ? `${numericText(telemetry.truth_error_cm)} cm`
      : "-",
  );
  setText("taskElapsed", task?.elapsed || "T+ 00:00.0");
  $("headingNeedle")?.parentElement?.style.setProperty("--heading-deg", `${yaw}deg`);
}

function renderEvidence(telemetry) {
  setText("frontMm", telemetry.front_mm);
  setText("leftMm", telemetry.left_mm);
  setText("rightMm", telemetry.right_mm);
  setText("encLeft", telemetry.enc_left, "0");
  setText("encRight", telemetry.enc_right, "0");
  setText(
    "pwmPair",
    `${valueText(telemetry.pwm_left, "0")} / ${valueText(telemetry.pwm_right, "0")}`,
  );
  setText("yawRate", telemetry.yaw_rate_dps);
  setText(
    "imuState",
    telemetry.imu_available
      ? `可用 · ${valueText(telemetry.imu_quality, "有效")}`
      : "未配置 / 降级",
  );
  const covariance = Array.isArray(telemetry.pose_covariance)
    ? telemetry.pose_covariance
    : [];
  setText(
    "poseCovariance",
    covariance.length === 3
      ? covariance.map((value) => numericText(value, 1)).join(" / ")
      : "-",
  );
  setText(
    "slipPair",
    Number.isFinite(Number(telemetry.slip_left))
    && Number.isFinite(Number(telemetry.slip_right))
      ? `${numericText(Number(telemetry.slip_left) * 100)}% / ${
          numericText(Number(telemetry.slip_right) * 100)
        }%`
      : "- / -",
  );
  setText("wallResidual", telemetry.wall_residual_mm);
  setText("networkLatency", telemetry.network_latency_ms);
  setText("streamLatency", `延迟 ${valueText(telemetry.video_latency_ms)} ms`);
  setText("batteryVoltage", telemetry.battery_v ? `${telemetry.battery_v} V` : "-- V");
  setText(
    "attitudeAlert",
    telemetry.attitude_alert || "无",
  );
}

function renderMap(maze) {
  setText("mazeMap", maze.ascii || "地图尚未建立", "");
}

function renderTimeline(logs, taskEvents) {
  const timeline = $("eventTimeline");
  const normalizedLogs = logs.map((row) => ({
    timestamp: row.ts_ms ? new Date(row.ts_ms) : null,
    type: row.type,
    payload: row.payload,
  }));
  const normalizedTasks = taskEvents.map((row) => ({
    timestamp: row.utc_timestamp ? new Date(row.utc_timestamp) : null,
    type: row.type,
    payload: row.payload,
  }));
  const events = [...normalizedLogs, ...normalizedTasks]
    .sort((left, right) => (right.timestamp?.getTime() || 0) - (left.timestamp?.getTime() || 0))
    .slice(0, 80);

  setText("logCount", `${events.length} 条`);
  if (!events.length) {
    const empty = document.createElement("li");
    empty.className = "event-empty";
    empty.textContent = "任务开始后，这里会按时间记录动作、定位和参数事件。";
    timeline.replaceChildren(empty);
    return;
  }
  timeline.replaceChildren(
    ...events.map((event) => {
      const item = document.createElement("li");
      const time = document.createElement("time");
      time.textContent = event.timestamp
        ? event.timestamp.toLocaleTimeString("zh-CN", { hour12: false })
        : "--:--:--";
      const title = document.createElement("strong");
      title.textContent = event.type || "event";
      const details = document.createElement("p");
      details.textContent = JSON.stringify(event.payload || {});
      item.append(time, title, details);
      return item;
    }),
  );
}

function renderTune(logs) {
  const lastTune = [...logs].reverse().find((row) => row.type === "param_change");
  setText("lastTuneReason", lastTune?.payload?.source || "-");
  setText(
    "lastTuneChange",
    lastTune ? JSON.stringify(lastTune.payload?.changes || {}) : "-",
  );
}

function renderParams(params, isController) {
  const rows = flattenParams(params.params || {});
  const fingerprint = `${params.param_version}:${isController}:${rows.length}`;
  if (fingerprint === parameterFingerprint) return;
  parameterFingerprint = fingerprint;

  const limits = params.limits || {};
  $("paramTable").replaceChildren(
    ...rows.map(([path, value]) => {
      const safetyLocked = path.startsWith("safety.");
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = path;
      const current = document.createElement("td");
      current.textContent = valueText(value);
      const unit = document.createElement("td");
      unit.textContent = unitFor(path);
      const range = document.createElement("td");
      range.textContent = Array.isArray(limits[path])
        ? `${limits[path][0]} … ${limits[path][1]}`
        : "未设限";
      const source = document.createElement("td");
      const sourceChip = document.createElement("span");
      sourceChip.className = `param-source ${safetyLocked ? "is-safety" : ""}`;
      sourceChip.textContent = safetyLocked ? "安全锁定" : `参数 v${params.param_version}`;
      source.append(sourceChip);
      const candidate = document.createElement("td");
      const input = document.createElement("input");
      input.className = "param-input";
      input.value = valueText(value);
      input.dataset.path = path;
      input.dataset.valueType = typeof value;
      input.type = typeof value === "number" ? "number" : "text";
      input.step = typeof value === "number" && Number.isInteger(value) ? "1" : "0.01";
      input.disabled = !isController || safetyLocked;
      candidate.append(input);
      const action = document.createElement("td");
      const button = document.createElement("button");
      button.className = "param-save";
      button.type = "button";
      button.dataset.saveParam = path;
      button.textContent = safetyLocked ? "需现场确认" : "形成并下发";
      button.disabled = !isController || safetyLocked;
      action.append(button);
      tr.append(name, current, unit, range, source, candidate, action);
      return tr;
    }),
  );

  setText(
    "wheelGeometry",
    `${valueText(params.params?.robot?.wheel_diameter_cm)} / ${valueText(
      params.params?.robot?.wheel_base_cm,
    )} cm`,
  );
  setText("robotMass", params.params?.robot?.mass_kg ? `${params.params.robot.mass_kg} kg` : "-- kg");
}
