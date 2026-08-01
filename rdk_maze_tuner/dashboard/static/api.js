import { getAppState } from "./state.js";

export class ApiError extends Error {
  constructor(message, status, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function request(
  path,
  {
    method = "GET",
    body,
    rawBody,
    contentType,
    control = false,
  } = {},
) {
  const appState = getAppState();
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (rawBody !== undefined) {
    headers["Content-Type"] = contentType || "application/octet-stream";
  }
  if (method !== "GET" && appState.csrfToken) {
    headers["X-CSRF-Token"] = appState.csrfToken;
  }
  if (control && appState.leaseToken) {
    headers["X-Control-Lease"] = appState.leaseToken;
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body:
      rawBody !== undefined
        ? rawBody
        : body === undefined
          ? undefined
          : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || `请求失败 (${response.status})`;
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

export function login(username, password) {
  return request("/api/auth/login", {
    method: "POST",
    body: { username, password },
  });
}

export function logout() {
  return request("/api/auth/logout", { method: "POST" });
}

export function currentUser() {
  return request("/api/auth/me");
}

export function fetchDashboardState() {
  return request("/api/state");
}

export function claimControl() {
  return request("/api/control/claim", { method: "POST" });
}

export function heartbeatControl() {
  return request("/api/control/heartbeat", {
    method: "POST",
    control: true,
  });
}

export function releaseControl() {
  return request("/api/control/release", {
    method: "POST",
    control: true,
  });
}

export function createTask(definition) {
  return request("/api/tasks", {
    method: "POST",
    body: definition,
    control: true,
  });
}

export function taskOperation(taskId, operation) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/${operation}`, {
    method: "POST",
    control: operation !== "estop",
  });
}

export function emergencyStop(reason = "dashboard") {
  return request("/api/command/estop", {
    method: "POST",
    body: { reason },
  });
}

export function manualAction(name) {
  return request("/api/command/action", {
    method: "POST",
    body: { name },
    control: true,
  });
}

export function updateParam(path, value) {
  return request("/api/params", {
    method: "POST",
    body: { updates: { [path]: value } },
    control: true,
  });
}

export function updateAutoTune(enabled) {
  return request("/api/auto-tune", {
    method: "POST",
    body: { enabled },
    control: true,
  });
}

export function listMaps() {
  return request("/api/maps");
}

export function listPhysicalProfiles() {
  return request("/api/physical-profiles");
}

export function listMapVersions(mapId) {
  return request(
    `/api/maps/${encodeURIComponent(mapId)}/versions`,
  );
}

export function getMapVersion(versionId) {
  return request(
    `/api/map-versions/${encodeURIComponent(versionId)}`,
  );
}

export function createMap(name, definition) {
  return request("/api/maps", {
    method: "POST",
    body: { name, definition },
    control: true,
  });
}

export function saveMapVersion(mapId, definition) {
  return request(
    `/api/maps/${encodeURIComponent(mapId)}/versions`,
    {
      method: "POST",
      body: { definition },
      control: true,
    },
  );
}

export function uploadMapSourceImage(mapId, file) {
  const filename = encodeURIComponent(file.name || "source-image");
  return request(
    `/api/maps/${encodeURIComponent(mapId)}/source-image?filename=${filename}`,
    {
      method: "POST",
      rawBody: file,
      contentType: file.type,
      control: true,
    },
  );
}

export function getRun(runId) {
  const encoded = encodeURIComponent(runId);
  return request(`/api/runs/${encoded}`);
}

export function listRuns(limit = 50) {
  return request(`/api/runs?limit=${encodeURIComponent(limit)}`);
}

export function getRunEvents(runId) {
  const encoded = encodeURIComponent(runId);
  return request(`/api/runs/${encoded}/events`);
}

export function getRunReplay(runId) {
  const encoded = encodeURIComponent(runId);
  return request(`/api/runs/${encoded}/replay`);
}

export function openStateSocket({ onOpen, onState, onClose, onError }) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  let pingTimer = null;

  socket.addEventListener("open", () => {
    onOpen?.();
    pingTimer = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, 250);
  });
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "state" || message.type === "pong") {
        onState?.(message.payload);
      }
    } catch (error) {
      onError?.(error);
    }
  });
  socket.addEventListener("error", (event) => onError?.(event));
  socket.addEventListener("close", () => {
    if (pingTimer) window.clearInterval(pingTimer);
    onClose?.();
  });
  return socket;
}
