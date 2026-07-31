const state = {
  latest: null,
  socket: null,
  pingTimer: null,
};

const $ = (id) => document.getElementById(id);

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
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

function render(payload) {
  state.latest = payload;
  const telemetry = payload.telemetry || {};
  $("connectionState").textContent = payload.connected ? "在线" : "离线";
  $("connectionState").className = `status ${payload.connected ? "status--online" : "status--offline"}`;
  $("esp32State").textContent = valueText(telemetry.state);
  $("paramVersion").textContent = `v${valueText(payload.params?.param_version)}`;
  $("poseLabel").textContent = `${payload.maze?.position?.join(",") || "0,0"} ${payload.maze?.heading || "N"}`;
  $("mazeMap").textContent = payload.maze?.ascii || "";
  $("frontMm").textContent = valueText(telemetry.front_mm);
  $("leftMm").textContent = valueText(telemetry.left_mm);
  $("rightMm").textContent = valueText(telemetry.right_mm);
  $("encLeft").textContent = valueText(telemetry.enc_left);
  $("encRight").textContent = valueText(telemetry.enc_right);
  $("pwmPair").textContent = `${valueText(telemetry.pwm_left)} / ${valueText(telemetry.pwm_right)}`;
  $("lastAck").textContent = payload.last_ack ? `ACK ${payload.last_ack.seq ?? "-"}` : "ACK -";
  $("currentAction").textContent = actionText(payload.current_action);
  $("autoTuneToggle").checked = Boolean(payload.auto_tune_enabled);
  renderParams(payload.params?.params || {});
  renderTune(payload.logs || []);
  renderLogs(payload.logs || []);
}

function actionText(action) {
  if (!action) return "-";
  const parts = [action.name || action.type || "-"];
  if (action.action_id) parts.push(action.action_id);
  if (action.type && action.type !== "action") parts.push(action.type);
  if (action.code) parts.push(action.code);
  return parts.join(" · ");
}

function renderParams(params) {
  const tbody = $("paramTable");
  const currentRows = flattenParams(params);
  tbody.replaceChildren(
    ...currentRows.map(([path, value]) => {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = path;
      const current = document.createElement("td");
      current.textContent = valueText(value);
      const next = document.createElement("td");
      const input = document.createElement("input");
      input.className = "param-input";
      input.value = valueText(value);
      input.dataset.path = path;
      input.type = typeof value === "number" ? "number" : "text";
      if (typeof value === "number" && !Number.isInteger(value)) input.step = "0.01";
      if (typeof value === "number" && Number.isInteger(value)) input.step = "1";
      next.append(input);
      const action = document.createElement("td");
      const button = document.createElement("button");
      button.className = "param-save";
      button.type = "button";
      button.textContent = "下发";
      button.addEventListener("click", () => saveParam(path, input.value, typeof value));
      action.append(button);
      tr.append(name, current, next, action);
      return tr;
    }),
  );
}

function renderTune(logs) {
  const lastTune = [...logs].reverse().find((row) => row.type === "param_change");
  if (!lastTune) {
    $("lastTuneReason").textContent = "-";
    $("lastTuneChange").textContent = "-";
    return;
  }
  $("lastTuneReason").textContent = lastTune.payload?.source || "-";
  $("lastTuneChange").textContent = JSON.stringify(lastTune.payload?.changes || {});
}

function renderLogs(logs) {
  $("logCount").textContent = String(logs.length);
  $("logList").replaceChildren(
    ...logs.slice(-80).reverse().map((row) => {
      const item = document.createElement("li");
      item.textContent = `${new Date(row.ts_ms).toLocaleTimeString()} ${row.type} ${JSON.stringify(row.payload)}`;
      return item;
    }),
  );
}

function coerceValue(raw, valueType) {
  if (valueType === "number") {
    return raw.includes(".") ? Number.parseFloat(raw) : Number.parseInt(raw, 10);
  }
  if (raw === "true") return true;
  if (raw === "false") return false;
  return raw;
}

async function saveParam(path, raw, valueType) {
  const value = coerceValue(raw, valueType);
  const response = await fetch("/api/params", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { [path]: value } }),
  });
  const payload = await response.json();
  $("paramResult").textContent = response.ok ? `已下发 ${path}` : payload.detail || "失败";
  await fetchState();
}

async function sendCommand(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await response.json().catch(() => ({}));
  await fetchState();
}

async function fetchState() {
  const response = await fetch("/api/state");
  if (!response.ok) return;
  render(await response.json());
}

function connectSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    if (state.pingTimer) window.clearInterval(state.pingTimer);
    state.pingTimer = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
    }, 300);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "state" || message.type === "pong") render(message.payload);
  });
  socket.addEventListener("close", () => {
    if (state.pingTimer) window.clearInterval(state.pingTimer);
    state.pingTimer = null;
    window.setTimeout(connectSocket, 1500);
  });
}

$("estopButton").addEventListener("click", () => sendCommand("/api/command/estop", { reason: "dashboard" }));
$("stopButton").addEventListener("click", () => sendCommand("/api/command/stop", { reason: "dashboard" }));
document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => sendCommand("/api/command/action", { name: button.dataset.action }));
});
$("autoTuneToggle").addEventListener("change", (event) => {
  sendCommand("/api/auto-tune", { enabled: event.target.checked });
});

fetchState();
connectSocket();
window.setInterval(fetchState, 1200);
