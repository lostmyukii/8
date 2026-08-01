import { ApiError, getRunReplay, listRuns } from "./api.js";

const $ = (id) => document.getElementById(id);

const CATEGORY_LABELS = {
  completion_goal: "完成与到达",
  map_accuracy: "地图准确",
  localization_heading: "定位与方向",
  path_time: "路径与时间",
  action_precision: "动作精度",
  safety_stability: "安全与稳定",
};

const model = {
  runId: "",
  manifest: null,
  loading: false,
  playing: false,
  playStartedAt: 0,
  playStartedMs: 0,
  currentMs: 0,
  frame: null,
  videoStarted: false,
  runsLoaded: false,
  runsLoading: false,
  runIds: new Set(),
};

export function initializeRunReplay() {
  $("replayPlayButton")?.addEventListener("click", togglePlayback);
  $("replaySeek")?.addEventListener("input", (event) => {
    pausePlayback();
    seekTo(Number(event.target.value));
  });
  $("replayVideo")?.addEventListener("ended", () => {
    pausePlayback();
    seekTo(durationMs());
  });
  $("replayRunSelect")?.addEventListener("change", (event) => {
    const runId = event.target.value;
    if (!runId || runId === model.runId) return;
    pausePlayback();
    model.runId = runId;
    loadReplay(runId);
  });
}

export function updateReplayTask(task) {
  const runId = task?.run_id || task?.last_run_id || "";
  ensureRunList(runId);
  if (!runId || runId === model.runId || model.loading) return;
  model.runId = runId;
  loadReplay(runId);
}

async function ensureRunList(preferredRunId = "") {
  if (
    preferredRunId
    && model.runsLoaded
    && !model.runIds.has(preferredRunId)
  ) {
    model.runsLoaded = false;
  }
  if (model.runsLoaded || model.runsLoading) return;
  model.runsLoading = true;
  try {
    const payload = await listRuns();
    const runs = Array.isArray(payload?.runs) ? payload.runs : [];
    model.runIds = new Set(runs.map((run) => run.run_id));
    renderRunOptions(runs, preferredRunId);
    model.runsLoaded = true;
    const selected = preferredRunId || runs[0]?.run_id || "";
    if (selected && !model.runId) {
      model.runId = selected;
      loadReplay(selected);
    }
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      setText("replayStatus", "历史运行列表读取失败");
    }
  } finally {
    model.runsLoading = false;
  }
}

function renderRunOptions(runs, preferredRunId) {
  const select = $("replayRunSelect");
  const options = runs.map((run) => {
    const option = document.createElement("option");
    option.value = run.run_id;
    const score = Number.isFinite(Number(run.latest_score?.total_score))
      ? ` · ${Number(run.latest_score.total_score).toFixed(1)} 分`
      : "";
    option.textContent = `${run.run_id} · ${run.status}${score}`;
    return option;
  });
  if (!options.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "暂无运行记录";
    options.push(empty);
  }
  select.replaceChildren(...options);
  const selected = preferredRunId || model.runId || runs[0]?.run_id || "";
  if (selected) select.value = selected;
}

async function loadReplay(runId) {
  model.loading = true;
  setText("replayStatus", "正在装载运行证据…");
  setText("replayRunId", runId);
  try {
    const manifest = await getRunReplay(runId);
    if (runId !== model.runId) return;
    model.manifest = manifest;
    $("replayRunSelect").value = runId;
    model.currentMs = 0;
    renderManifest();
    seekTo(0);
  } catch (error) {
    if (runId !== model.runId) return;
    model.manifest = null;
    setText(
      "replayStatus",
      error instanceof ApiError && error.status === 404
        ? "运行证据尚在生成"
        : "回放装载失败",
    );
    setText("replayEventDetail", error.message || "无法读取回放证据");
    $("replayPlayButton").disabled = true;
  } finally {
    model.loading = false;
  }
}

function renderManifest() {
  const manifest = model.manifest;
  const duration = durationMs();
  const media = manifest?.media || {};
  const score = manifest?.score;
  const video = $("replayVideo");
  const fallback = $("replayStructuredFallback");

  setText(
    "replayStatus",
    `${statusText(manifest?.status)} · ${manifest?.timeline?.length || 0} 条事件`,
  );
  const mediaState = $("replayMediaState");
  mediaState.textContent = media.complete ? "VIDEO READY" : "STRUCTURED";
  mediaState.className = `status-pill ${
    media.complete ? "status-pill--online" : "status-pill--lost"
  }`;

  if (media.complete && media.url) {
    video.src = media.url;
    video.hidden = false;
    fallback.hidden = true;
  } else {
    video.removeAttribute("src");
    video.load();
    video.hidden = true;
    fallback.hidden = false;
    fallback.querySelector("strong").textContent = "结构化回放可用";
  }

  setText(
    "scoreTotal",
    Number.isFinite(Number(score?.total_score))
      ? Number(score.total_score).toFixed(1)
      : "--",
  );
  setText("scoreProfile", score?.profile_version || "等待原始指标");
  renderBreakdown(score?.breakdown || {});
  renderKeyEvents(manifest?.key_events || [], duration);

  const seek = $("replaySeek");
  seek.max = String(Math.max(0, Math.round(duration)));
  seek.value = "0";
  $("replayPlayButton").disabled =
    !Array.isArray(manifest?.timeline) || manifest.timeline.length === 0;
}

function renderBreakdown(breakdown) {
  const rows = Object.entries(CATEGORY_LABELS).map(([key, label]) => {
    const item = breakdown[key] || {};
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const value = document.createElement("dd");
    const evidence = item.evidence_complete === false ? " · 缺证据" : "";
    term.textContent = label;
    value.textContent = `${numberText(item.score)} / ${numberText(item.weight)}${evidence}`;
    row.classList.toggle("is-incomplete", item.evidence_complete === false);
    row.append(term, value);
    return row;
  });
  $("scoreBreakdown").replaceChildren(...rows);
}

function renderKeyEvents(events, duration) {
  const root = $("replayKeyEvents");
  root.replaceChildren(
    ...events.map((event) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `replay-notch replay-notch--${event.channel || "task"}`;
      button.style.left = `${percent(event.t_ms, duration)}%`;
      button.title = `${formatTime(event.t_ms)} · ${event.type}`;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", () => {
        pausePlayback();
        seekTo(event.t_ms);
      });
      return button;
    }),
  );
}

function togglePlayback() {
  if (model.playing) {
    pausePlayback();
    return;
  }
  if (model.currentMs >= durationMs()) seekTo(0);
  model.playing = true;
  model.playStartedAt = performance.now();
  model.playStartedMs = model.currentMs;
  model.videoStarted = false;
  $("replayPlayButton").textContent = "暂停";
  if (!$("replayVideo").hidden && videoIsActive(model.currentMs)) {
    $("replayVideo").currentTime = videoTimeSeconds(model.currentMs);
    $("replayVideo").play().catch(() => {});
    model.videoStarted = true;
  }
  model.frame = window.requestAnimationFrame(playFrame);
}

function playFrame(now) {
  if (!model.playing) return;
  const next = Math.min(
    durationMs(),
    model.playStartedMs + now - model.playStartedAt,
  );
  seekTo(next, { syncVideo: false });
  synchronizePlayingVideo(next);
  if (next >= durationMs()) {
    pausePlayback();
    return;
  }
  model.frame = window.requestAnimationFrame(playFrame);
}

function pausePlayback() {
  model.playing = false;
  if (model.frame) window.cancelAnimationFrame(model.frame);
  model.frame = null;
  model.videoStarted = false;
  $("replayPlayButton").textContent = "播放";
  $("replayVideo")?.pause();
}

function seekTo(value, { syncVideo = true } = {}) {
  const duration = durationMs();
  model.currentMs = Math.max(0, Math.min(Number(value) || 0, duration));
  $("replaySeek").value = String(Math.round(model.currentMs));
  positionPlayhead(percent(model.currentMs, duration));
  setText(
    "replayTime",
    `${formatTime(model.currentMs)} / ${formatTime(duration)}`,
  );
  if (syncVideo && !$("replayVideo").hidden) {
    $("replayVideo").currentTime = videoTimeSeconds(model.currentMs);
  }
  renderCurrentEvent();
}

function renderCurrentEvent() {
  const events = model.manifest?.timeline || [];
  const current = [...events]
    .reverse()
    .find((event) => Number(event.t_ms) <= model.currentMs);
  if (!current) {
    setText("replayEventDetail", "时间轴起点 · 等待第一条事件");
    return;
  }
  setText(
    "replayEventDetail",
    `${formatTime(current.t_ms)} · ${current.type} · ${current.source} · ${
      JSON.stringify(current.payload || {})
    }`,
  );
}

function durationMs() {
  return Math.max(0, Number(model.manifest?.duration_ms) || 0);
}

function videoOffsetMs() {
  return Number(model.manifest?.media?.timeline_offset_ms) || 0;
}

function videoTimeSeconds(timelineMs) {
  return Math.max(0, Number(timelineMs) - videoOffsetMs()) / 1000;
}

function videoIsActive(timelineMs) {
  return Number(timelineMs) >= videoOffsetMs();
}

function synchronizePlayingVideo(timelineMs) {
  const video = $("replayVideo");
  if (video.hidden || !videoIsActive(timelineMs)) {
    if (!video.hidden) video.pause();
    return;
  }
  const expected = videoTimeSeconds(timelineMs);
  if (Math.abs(video.currentTime - expected) > 0.25) {
    video.currentTime = expected;
  }
  if (!model.videoStarted) {
    video.play().catch(() => {});
    model.videoStarted = true;
  }
}

function formatTime(milliseconds) {
  const safe = Math.max(0, Number(milliseconds) || 0);
  const minutes = Math.floor(safe / 60_000);
  const seconds = Math.floor((safe % 60_000) / 1000);
  const millis = Math.floor(safe % 1000);
  return `${String(minutes).padStart(2, "0")}:${
    String(seconds).padStart(2, "0")
  }.${String(millis).padStart(3, "0")}`;
}

function percent(value, total) {
  return total > 0
    ? Math.max(0, Math.min(100, Number(value) / total * 100))
    : 0;
}

function numberText(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(1) : "--";
}

function positionPlayhead(progress) {
  const rail = $("replayRail");
  const playhead = $("replayPlayhead");
  if (!rail || !playhead) return;
  const start = window.matchMedia("(max-width: 768px)").matches ? 58 : 72;
  const end = 14;
  const available = Math.max(0, rail.clientWidth - start - end);
  playhead.style.left = `${start + available * progress / 100}px`;
}

function statusText(status) {
  const labels = {
    COMPLETED: "运行完成",
    ERROR: "运行异常",
    ESTOP: "急停结束",
    LOST: "连接丢失",
  };
  return labels[status] || status || "运行记录";
}

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = String(value ?? "-");
}
