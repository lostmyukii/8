const storage = window.sessionStorage;

const model = {
  payload: null,
  authenticated: false,
  csrfToken: storage.getItem("maze.csrf") || "",
  leaseToken: storage.getItem("maze.lease") || "",
  selectedMode: storage.getItem("maze.mode") || "simulation",
  selectedPhysicalProfileId:
    storage.getItem("maze.physical-profile") || "normal-v1",
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
  storage.removeItem("maze.csrf");
  storage.removeItem("maze.lease");
  storage.removeItem("maze.task");
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
