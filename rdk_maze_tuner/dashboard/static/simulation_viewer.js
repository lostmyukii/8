const VIEWER_SOURCE = "maze-simulation-viewer";
const STREAM_PATH = "/simulation/";
const DEFAULT_THUMBNAIL =
  "https://cyberbotics.com/wwi/R2025a/images/loading/default_thumbnail.png";
const MOBILE_DEVICE =
  /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
    navigator.userAgent,
  );

const webotsView = document.getElementById("webotsView");
const status = document.getElementById("viewerStatus");
let retryTimer = null;
let retryDelayMs = 1000;
let closing = false;
let connecting = false;

function notify(type, details = {}) {
  window.parent.postMessage(
    { source: VIEWER_SOURCE, type, ...details },
    window.location.origin,
  );
}

function showStatus(message, state = "connecting") {
  status.textContent = message;
  status.dataset.state = state;
}

function streamUrl() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}${STREAM_PATH}`;
}

function scheduleReconnect() {
  if (closing || retryTimer) return;
  showStatus("Webots 已断开，正在自动重连…", "reconnecting");
  notify("maze.webots.reconnecting", { retry_delay_ms: retryDelayMs });
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    connect();
  }, retryDelayMs);
  retryDelayMs = Math.min(retryDelayMs * 2, 10_000);
}

function connect() {
  if (closing || connecting) return;
  connecting = true;
  showStatus("正在连接 Webots 物理仿真…");
  try {
    webotsView.onready = () => {
      connecting = false;
      retryDelayMs = 1000;
      showStatus("Webots 物理仿真已连接", "ready");
      notify("maze.webots.ready");
    };
    webotsView.ondisconnect = () => {
      connecting = false;
      scheduleReconnect();
    };
    webotsView.connect(
      streamUrl(),
      "w3d",
      false,
      MOBILE_DEVICE,
      -1,
      DEFAULT_THUMBNAIL,
    );
  } catch (error) {
    connecting = false;
    showStatus("Webots 连接失败，正在重试…", "error");
    notify("maze.webots.error", {
      message: String(error?.message || error || "connection failed"),
    });
    scheduleReconnect();
  }
}

window.addEventListener("beforeunload", () => {
  closing = true;
  if (retryTimer) window.clearTimeout(retryTimer);
  try {
    webotsView.close();
  } catch {
    // The viewer can be unloaded before its custom element is fully ready.
  }
});

customElements
  .whenDefined("webots-view")
  .then(connect)
  .catch((error) => {
    showStatus("无法加载 Webots 查看器", "error");
    notify("maze.webots.error", {
      message: String(error?.message || error || "viewer load failed"),
    });
  });
