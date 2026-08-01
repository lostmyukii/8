import {
  ApiError,
  currentUser,
  fetchDashboardState,
  openStateSocket,
} from "./api.js";
import {
  getAppState,
  setAuthenticated,
  setPayload,
  setSocketConnected,
  subscribe,
} from "./state.js";
import {
  renderAuthGate,
  renderDashboard,
  showNotice,
} from "./render.js";
import { bindControls } from "./controls.js";
import { initializeMazeEditor } from "./maze_editor.js";
import {
  initializeRunReplay,
  updateReplayTask,
} from "./replay.js";

let socket = null;
let reconnectTimer = null;
let pollTimer = null;
let renderTimer = null;
let lastRenderAt = 0;

function scheduleRender(appState) {
  const elapsed = performance.now() - lastRenderAt;
  if (renderTimer) window.clearTimeout(renderTimer);
  renderTimer = window.setTimeout(
    () => {
      lastRenderAt = performance.now();
      const current = getAppState();
      renderDashboard(current);
      if (current.authenticated) {
        updateReplayTask(current.activeTask);
      }
    },
    Math.max(0, 100 - elapsed),
  );
}

subscribe(scheduleRender);
renderAuthGate(false);

async function refreshState() {
  const payload = await fetchDashboardState();
  setPayload(payload);
  return payload;
}

function stopLiveUpdates() {
  if (socket) socket.close();
  socket = null;
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
  setSocketConnected(false);
}

function startLiveUpdates() {
  if (!getAppState().authenticated || socket) return;
  socket = openStateSocket({
    onOpen: () => {
      setSocketConnected(true);
    },
    onState: (payload) => {
      setPayload(payload);
    },
    onError: () => {
      setSocketConnected(false);
    },
    onClose: () => {
      socket = null;
      setSocketConnected(false);
      if (getAppState().authenticated) {
        reconnectTimer = window.setTimeout(startLiveUpdates, 1500);
      }
    },
  });
  if (!pollTimer) {
    pollTimer = window.setInterval(() => {
      if (!getAppState().authenticated) return;
      refreshState().catch((error) => {
        if (error instanceof ApiError && error.status === 401) {
          stopLiveUpdates();
          setAuthenticated(false);
        }
      });
    }, 1200);
  }
}

bindControls({
  refreshState,
  onAuthenticated: startLiveUpdates,
  onLogout: stopLiveUpdates,
});
initializeMazeEditor();
initializeRunReplay();

async function bootstrap() {
  try {
    await currentUser();
    setAuthenticated(true);
    await refreshState();
    startLiveUpdates();
  } catch (error) {
    stopLiveUpdates();
    setAuthenticated(false);
    if (!(error instanceof ApiError && error.status === 401)) {
      showNotice(error.message || "任务台初始化失败", { error: true });
    }
  }
}

bootstrap();
