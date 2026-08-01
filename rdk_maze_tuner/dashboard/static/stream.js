const VIEWER_SOURCE = "maze-simulation-viewer";

let initialized = false;

function elements() {
  return {
    frame: document.getElementById("streamFrame"),
    iframe: document.getElementById("simulationViewer"),
    placeholder: document.getElementById("streamPlaceholder"),
    title: document.getElementById("streamTitle"),
    hint: document.getElementById("streamHint"),
  };
}

function setReady(ready) {
  const { frame } = elements();
  frame?.classList.toggle("is-stream-ready", Boolean(ready));
}

function handleViewerMessage(event) {
  const { iframe, title, hint } = elements();
  if (!iframe || event.origin !== window.location.origin) return;
  if (event.source !== iframe.contentWindow) return;
  if (event.data?.source !== VIEWER_SOURCE) return;

  if (event.data.type === "maze.webots.ready") {
    setReady(true);
    if (title) title.textContent = "Webots 实时画面";
    if (hint) hint.textContent = "W3D 物理仿真已连接";
    return;
  }

  setReady(false);
  if (event.data.type === "maze.webots.reconnecting") {
    if (title) title.textContent = "Webots 正在重连";
    if (hint) hint.textContent = "仿真服务短暂断开，正在自动恢复";
  } else if (event.data.type === "maze.webots.error") {
    if (title) title.textContent = "Webots 连接失败";
    if (hint) hint.textContent = "请检查仿真服务与认证状态";
  }
}

export function initializeLiveStream() {
  if (initialized) return;
  window.addEventListener("message", handleViewerMessage);
  initialized = true;
}

export function updateLiveStream(appState) {
  const { iframe, frame } = elements();
  if (!iframe || !frame) return;

  const selectedMode =
    appState.activeTask?.mode || appState.selectedMode || "simulation";
  const shouldConnect =
    Boolean(appState.authenticated) && selectedMode === "simulation";

  frame.dataset.mode = selectedMode;
  if (shouldConnect) {
    if (iframe.src === "about:blank" || !iframe.src) {
      iframe.src = iframe.dataset.src;
      setReady(false);
    }
    return;
  }

  if (iframe.src !== "about:blank") {
    iframe.src = "about:blank";
  }
  setReady(false);
}
