const storage = window.sessionStorage;

const model = {
  payload: null,
  authenticated: false,
  csrfToken: storage.getItem("maze.csrf") || "",
  leaseToken: storage.getItem("maze.lease") || "",
  selectedMode: storage.getItem("maze.mode") || "simulation",
  selectedPhysicalProfileId:
    storage.getItem("maze.physical-profile") || "normal-v1",
  selectedMapVersionId: storage.getItem("maze.map-version") || "",
  mapVersionStatus: "idle",
  mapVersionDetail: null,
  mapGoal: null,
  mapVersionError: "",
  physicalProfiles: [],
  physicalProfilesLoaded: false,
  activeTaskId: storage.getItem("maze.task") || "",
  socketConnected: false,
};

const listeners = new Set();

function notify() {
  const snapshot = getAppState();
  listeners.forEach((listener) => listener(snapshot));
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getAppState() {
  return {
    ...model,
    activeTask: getActiveTask(),
  };
}

export function setAuthenticated(authenticated) {
  model.authenticated = Boolean(authenticated);
  notify();
}

export function setAuthSession({ csrfToken = "", user = null } = {}) {
  model.authenticated = Boolean(user);
  model.csrfToken = csrfToken;
  if (csrfToken) storage.setItem("maze.csrf", csrfToken);
  notify();
}

export function clearAuthSession() {
  model.payload = null;
  model.authenticated = false;
  model.csrfToken = "";
  model.leaseToken = "";
  model.activeTaskId = "";
  model.socketConnected = false;
  model.selectedMapVersionId = "";
  model.mapVersionStatus = "idle";
  model.mapVersionDetail = null;
  model.mapGoal = null;
  model.mapVersionError = "";
  storage.removeItem("maze.csrf");
  storage.removeItem("maze.lease");
  storage.removeItem("maze.task");
  storage.removeItem("maze.map-version");
  notify();
}

export function setLeaseToken(token) {
  model.leaseToken = token || "";
  if (model.leaseToken) {
    storage.setItem("maze.lease", model.leaseToken);
  } else {
    storage.removeItem("maze.lease");
  }
  notify();
}

export function setSelectedMode(mode) {
  if (!["simulation", "real"].includes(mode)) return;
  model.selectedMode = mode;
  storage.setItem("maze.mode", mode);
  notify();
}

export function setPhysicalProfiles(profiles) {
  model.physicalProfiles = Array.isArray(profiles) ? profiles : [];
  model.physicalProfilesLoaded = true;
  if (
    !model.physicalProfiles.some(
      (profile) => profile.profile_id === model.selectedPhysicalProfileId,
    )
    && model.physicalProfiles.length
  ) {
    model.selectedPhysicalProfileId = model.physicalProfiles[0].profile_id;
    storage.setItem(
      "maze.physical-profile",
      model.selectedPhysicalProfileId,
    );
  }
  notify();
}

export function setSelectedPhysicalProfile(profileId) {
  const normalized = String(profileId || "").trim();
  if (!normalized) return;
  model.selectedPhysicalProfileId = normalized;
  storage.setItem("maze.physical-profile", normalized);
  notify();
}

export function setMapVersionLoading(versionId) {
  const normalized = String(versionId || "").trim();
  model.selectedMapVersionId = normalized;
  model.mapVersionStatus = normalized ? "loading" : "idle";
  model.mapVersionDetail = null;
  model.mapGoal = null;
  model.mapVersionError = "";
  if (normalized) {
    storage.setItem("maze.map-version", normalized);
  } else {
    storage.removeItem("maze.map-version");
  }
  notify();
}

export function setMapVersionDetail(version) {
  const versionId = String(version?.version_id || "").trim();
  if (!versionId || versionId !== model.selectedMapVersionId) return false;
  model.mapGoal = resolveAutomaticGoal(version);
  model.mapVersionDetail = version;
  model.mapVersionStatus = "ready";
  model.mapVersionError = "";
  notify();
  return true;
}

export function setMapVersionError(versionId, error) {
  if (String(versionId || "").trim() !== model.selectedMapVersionId) {
    return false;
  }
  model.mapVersionStatus = "error";
  model.mapVersionDetail = null;
  model.mapGoal = null;
  model.mapVersionError = error?.message || String(error || "地图版本加载失败");
  notify();
  return true;
}

export function setActiveTask(taskId) {
  model.activeTaskId = taskId || "";
  if (model.activeTaskId) {
    storage.setItem("maze.task", model.activeTaskId);
  } else {
    storage.removeItem("maze.task");
  }
  notify();
}

export function setPayload(payload) {
  model.payload = payload || null;
  model.authenticated = Boolean(payload?.auth?.user);
  const tasks = payload?.tasks || [];
  const selected = tasks.find((task) => task.task_id === model.activeTaskId);
  if (!selected && tasks.length) {
    const latest = [...tasks].sort((left, right) =>
      String(right.created_at_utc).localeCompare(String(left.created_at_utc)),
    )[0];
    model.activeTaskId = latest.task_id;
    storage.setItem("maze.task", latest.task_id);
  }
  const active = getActiveTask();
  if (active?.mode) {
    model.selectedMode = active.mode;
    storage.setItem("maze.mode", active.mode);
  }
  const profileLocked = Boolean(
    active
    && !["IDLE", "COMPLETED", "LOST", "ERROR", "ESTOP"].includes(
      active.status,
    ),
  );
  if (
    active?.mode === "simulation"
    && active.physical_profile_id
    && profileLocked
  ) {
    model.selectedPhysicalProfileId = active.physical_profile_id;
    storage.setItem(
      "maze.physical-profile",
      active.physical_profile_id,
    );
  }
  notify();
}

export function setSocketConnected(connected) {
  model.socketConnected = Boolean(connected);
  notify();
}

export function getActiveTask() {
  const tasks = model.payload?.tasks || [];
  return (
    tasks.find((task) => task.task_id === model.activeTaskId) ||
    tasks.at(-1) ||
    null
  );
}

function resolveAutomaticGoal(version) {
  const definition = version?.definition || {};
  const rows = Number(definition.rows);
  const cols = Number(definition.cols);
  const start = [
    Number(definition.start?.x),
    Number(definition.start?.y),
  ];
  const candidates = (definition.goals || [])
    .map((goal) => [Number(goal.x), Number(goal.y)])
    .sort((left, right) => left[1] - right[1] || left[0] - right[0]);
  if (
    !Number.isInteger(rows)
    || !Number.isInteger(cols)
    || rows < 1
    || cols < 1
    || !cellInBounds(start, rows, cols)
  ) {
    throw new Error("地图起点或尺寸无效");
  }
  if (!candidates.length || candidates.some(
    (cell) => !cellInBounds(cell, rows, cols),
  )) {
    throw new Error("地图没有有效的自动终点");
  }

  const wallKeys = new Set(
    (definition.walls || []).map((wall) => (
      Number(wall.y1) === Number(wall.y2)
        ? `H:${Number(wall.y1)}:${Number(wall.x1)}`
        : `V:${Number(wall.x1)}:${Number(wall.y1)}`
    )),
  );
  const distances = new Map([[cellKey(start), 0]]);
  const queue = [start];
  const directions = [
    ["N", 0, -1],
    ["E", 1, 0],
    ["S", 0, 1],
    ["W", -1, 0],
  ];
  for (let index = 0; index < queue.length; index += 1) {
    const cell = queue[index];
    for (const [direction, dx, dy] of directions) {
      if (blocked(wallKeys, cell, direction)) continue;
      const neighbor = [cell[0] + dx, cell[1] + dy];
      const key = cellKey(neighbor);
      if (!cellInBounds(neighbor, rows, cols) || distances.has(key)) {
        continue;
      }
      distances.set(key, distances.get(cellKey(cell)) + 1);
      queue.push(neighbor);
    }
  }
  const reachable = candidates
    .filter((cell) => distances.has(cellKey(cell)))
    .sort((left, right) => (
      distances.get(cellKey(left)) - distances.get(cellKey(right))
      || left[1] - right[1]
      || left[0] - right[0]
    ));
  if (!reachable.length) {
    throw new Error("地图终点从起点不可达");
  }
  const selected = reachable[0];
  return {
    type: "map_goal",
    cell: selected,
    candidate_cells: candidates,
    source_map_version: version.version_id,
    source_map_digest: version.digest,
    resolution: candidates.length === 1 ? "single" : "shortest_path",
    path_length_cells: distances.get(cellKey(selected)),
  };
}

function cellKey(cell) {
  return `${cell[0]},${cell[1]}`;
}

function cellInBounds(cell, rows, cols) {
  return (
    Number.isInteger(cell[0])
    && Number.isInteger(cell[1])
    && cell[0] >= 0
    && cell[0] < cols
    && cell[1] >= 0
    && cell[1] < rows
  );
}

function blocked(walls, cell, direction) {
  const [x, y] = cell;
  const key = {
    N: `H:${y}:${x}`,
    E: `V:${x + 1}:${y}`,
    S: `H:${y + 1}:${x}`,
    W: `V:${x}:${y}`,
  }[direction];
  return walls.has(key);
}
