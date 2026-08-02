import {
  ApiError,
  createMap,
  getMapVersion,
  listMapVersions,
  listMaps,
  saveMapVersion,
  uploadMapSourceImage,
} from "./api.js";
import { getAppState, subscribe } from "./state.js";
import { showNotice } from "./render.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

const draft = {
  rows: 5,
  cols: 5,
  walls: new Set(),
  start: { x: 0, y: 4 },
  goals: [{ x: 4, y: 0 }],
  tool: "draw",
  pointerActive: false,
  lastEdge: "",
  undo: [],
  redo: [],
  calibrationPoints: [],
  imageFile: null,
  imageUrl: "",
  imageDigest: null,
  currentMapId: "",
  currentVersionId: "",
  initialized: false,
  mapsLoaded: false,
};

function edgeKey(orientation, fixed, offset) {
  return `${orientation}:${fixed}:${offset}`;
}

function parseEdge(key) {
  const [orientation, fixed, offset] = key.split(":");
  const a = Number(fixed);
  const b = Number(offset);
  if (orientation === "H") {
    return { x1: b, y1: a, x2: b + 1, y2: a };
  }
  return { x1: a, y1: b, x2: a, y2: b + 1 };
}

function boundaryWalls(rows, cols) {
  const walls = new Set();
  for (let x = 0; x < cols; x += 1) {
    walls.add(edgeKey("H", 0, x));
    walls.add(edgeKey("H", rows, x));
  }
  for (let y = 0; y < rows; y += 1) {
    walls.add(edgeKey("V", 0, y));
    walls.add(edgeKey("V", cols, y));
  }
  return walls;
}

function inputNumber(id, fallback) {
  const value = Number.parseInt($(id).value, 10);
  return Number.isFinite(value) ? value : fallback;
}

function currentDimensions() {
  return {
    rows: Math.max(1, Math.min(64, inputNumber("mazeRows", draft.rows))),
    cols: Math.max(1, Math.min(64, inputNumber("mazeCols", draft.cols))),
  };
}

function snapshotDraft() {
  return {
    rows: draft.rows,
    cols: draft.cols,
    walls: [...draft.walls],
    start: { ...draft.start },
    goals: draft.goals.map((goal) => ({ ...goal })),
    heading: $("mazeHeading").value,
  };
}

function restoreDraft(snapshot) {
  draft.rows = snapshot.rows;
  draft.cols = snapshot.cols;
  draft.walls = new Set(snapshot.walls);
  draft.start = { ...snapshot.start };
  draft.goals = snapshot.goals.map((goal) => ({ ...goal }));
  $("mazeRows").value = String(draft.rows);
  $("mazeCols").value = String(draft.cols);
  $("mazeHeading").value = snapshot.heading;
  render();
}

function remember() {
  draft.undo.push(snapshotDraft());
  if (draft.undo.length > 100) draft.undo.shift();
  draft.redo.length = 0;
  updateHistoryButtons();
}

function undo() {
  if (!draft.undo.length) return;
  draft.redo.push(snapshotDraft());
  restoreDraft(draft.undo.pop());
  markDirty("已撤销上一步");
}

function redo() {
  if (!draft.redo.length) return;
  draft.undo.push(snapshotDraft());
  restoreDraft(draft.redo.pop());
  markDirty("已重做一步");
}

function updateHistoryButtons() {
  $("mazeUndo").disabled = !draft.undo.length;
  $("mazeRedo").disabled = !draft.redo.length;
}

function setTool(tool) {
  draft.tool = tool;
  draft.calibrationPoints = tool === "calibrate"
    ? []
    : draft.calibrationPoints;
  const buttons = {
    draw: "mazeToolDraw",
    erase: "mazeToolErase",
    start: "mazeSetStart",
    goal: "mazeSetGoal",
    calibrate: "mazeCalibrate",
  };
  Object.entries(buttons).forEach(([name, id]) => {
    $(id).classList.toggle("is-active", name === tool);
  });
  const instructions = {
    draw: "沿格线拖动描墙；墙体会吸附为单位墙段。",
    erase: "沿现有墙体拖动擦除；保存时仍会检查外边界闭合。",
    start: "点击一个格子设置起点，再选择初始车头方向。",
    goal: "点击格子切换终点；至少保留一个终点。",
    calibrate: "在底图上依次点击两个同水平或同垂直的已知点。",
  };
  editorMessage(instructions[tool]);
  render();
}

function editorPoint(event) {
  const rect = $("mazeCanvas").getBoundingClientRect();
  return {
    x: ((event.clientX - rect.left) / rect.width) * draft.cols,
    y: ((event.clientY - rect.top) / rect.height) * draft.rows,
  };
}

function snappedEdge(point) {
  const verticalDistance = Math.abs(point.x - Math.round(point.x));
  const horizontalDistance = Math.abs(point.y - Math.round(point.y));
  if (verticalDistance < horizontalDistance) {
    const x = Math.max(0, Math.min(draft.cols, Math.round(point.x)));
    const y = Math.max(
      0,
      Math.min(draft.rows - 1, Math.floor(point.y)),
    );
    return edgeKey("V", x, y);
  }
  const y = Math.max(0, Math.min(draft.rows, Math.round(point.y)));
  const x = Math.max(
    0,
    Math.min(draft.cols - 1, Math.floor(point.x)),
  );
  return edgeKey("H", y, x);
}

function cellAt(point) {
  return {
    x: Math.max(0, Math.min(draft.cols - 1, Math.floor(point.x))),
    y: Math.max(0, Math.min(draft.rows - 1, Math.floor(point.y))),
  };
}

function applyEdge(key) {
  if (!key || key === draft.lastEdge) return;
  const present = draft.walls.has(key);
  if (
    (draft.tool === "draw" && present)
    || (draft.tool === "erase" && !present)
  ) {
    draft.lastEdge = key;
    return;
  }
  remember();
  if (draft.tool === "draw") draft.walls.add(key);
  if (draft.tool === "erase") draft.walls.delete(key);
  draft.lastEdge = key;
  markDirty();
  render();
}

function placeCell(point) {
  const cell = cellAt(point);
  remember();
  if (draft.tool === "start") {
    draft.start = cell;
    draft.goals = draft.goals.filter(
      (goal) => goal.x !== cell.x || goal.y !== cell.y,
    );
    if (!draft.goals.length) {
      draft.goals = [{
        x: Math.min(draft.cols - 1, cell.x + 1),
        y: cell.y,
      }];
    }
    markDirty("起点已更新");
  } else if (draft.tool === "goal") {
    const index = draft.goals.findIndex(
      (goal) => goal.x === cell.x && goal.y === cell.y,
    );
    if (index >= 0 && draft.goals.length > 1) {
      draft.goals.splice(index, 1);
    } else if (
      index < 0
      && (cell.x !== draft.start.x || cell.y !== draft.start.y)
    ) {
      draft.goals.push(cell);
    }
    markDirty("终点已更新");
  }
  render();
}

function calibrate(point) {
  draft.calibrationPoints.push(point);
  if (draft.calibrationPoints.length < 2) {
    editorMessage("已记录第一个标定点，请点击第二个点。");
    render();
    return;
  }
  const [first, second] = draft.calibrationPoints;
  const knownLength = inputNumber("mazeCalibrationLength", 0);
  const dx = Math.abs(second.x - first.x);
  const dy = Math.abs(second.y - first.y);
  if (knownLength <= 0 || (dx > 0.12 && dy > 0.12)) {
    editorMessage(
      "标定点必须大致水平或垂直，且实际长度必须大于 0。",
      true,
    );
    draft.calibrationPoints = [];
    render();
    return;
  }
  remember();
  if (dx >= dy && dx > 0.05) {
    $("mazeCellWidth").value = String(Math.round(knownLength / dx));
  } else if (dy > 0.05) {
    $("mazeCellHeight").value = String(Math.round(knownLength / dy));
  } else {
    editorMessage("两个标定点距离过近。", true);
    draft.calibrationPoints = [];
    render();
    return;
  }
  draft.calibrationPoints = [];
  markDirty("底图比例已写入格子尺寸，请检查后保存。");
  render();
}

function handlePointerDown(event) {
  if (!canEdit()) return;
  const point = editorPoint(event);
  if (["start", "goal"].includes(draft.tool)) {
    placeCell(point);
    return;
  }
  if (draft.tool === "calibrate") {
    calibrate(point);
    return;
  }
  draft.pointerActive = true;
  draft.lastEdge = "";
  $("mazeCanvas").setPointerCapture?.(event.pointerId);
  applyEdge(snappedEdge(point));
}

function handlePointerMove(event) {
  if (!draft.pointerActive || !["draw", "erase"].includes(draft.tool)) {
    return;
  }
  applyEdge(snappedEdge(editorPoint(event)));
}

function handlePointerUp(event) {
  draft.pointerActive = false;
  draft.lastEdge = "";
  $("mazeCanvas").releasePointerCapture?.(event.pointerId);
}

function applyGridSize() {
  if (!canEdit()) return;
  remember();
  const { rows, cols } = currentDimensions();
  draft.rows = rows;
  draft.cols = cols;
  draft.walls = boundaryWalls(rows, cols);
  draft.start = { x: 0, y: rows - 1 };
  draft.goals = [{ x: cols - 1, y: 0 }];
  markDirty("尺寸已应用，外边界已重建。");
  render();
}

function buildDefinition() {
  return {
    rows: draft.rows,
    cols: draft.cols,
    cell_width_mm: inputNumber("mazeCellWidth", 300),
    cell_height_mm: inputNumber("mazeCellHeight", 300),
    wall_thickness_mm: inputNumber("mazeWallThickness", 18),
    wall_height_mm: inputNumber("mazeWallHeight", 120),
    start: {
      ...draft.start,
      heading: $("mazeHeading").value,
    },
    goals: draft.goals
      .map((goal) => ({ ...goal }))
      .sort((left, right) => left.y - right.y || left.x - right.x),
    walls: [...draft.walls]
      .sort()
      .map(parseEdge),
    source_image_digest: draft.imageDigest,
  };
}

async function fileDigest(file) {
  if (!file) return null;
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function save() {
  if (!canEdit()) {
    editorMessage("请先取得控制权再保存地图版本。", true);
    return;
  }
  const button = $("mazeSaveVersion");
  button.disabled = true;
  try {
    draft.imageDigest = await fileDigest(draft.imageFile);
    const definition = buildDefinition();
    let result;
    if (draft.currentMapId) {
      result = await saveMapVersion(draft.currentMapId, definition);
    } else {
      result = await createMap($("mazeName").value.trim(), definition);
      draft.currentMapId = result.map.map_id;
    }
    const version = result.version;
    if (draft.imageFile) {
      const uploaded = await uploadMapSourceImage(
        draft.currentMapId,
        draft.imageFile,
      );
      if (uploaded.artifact.sha256 !== draft.imageDigest) {
        throw new Error("底图上传 digest 与本地计算不一致");
      }
    }
    draft.currentVersionId = version.version_id;
    draft.undo.length = 0;
    draft.redo.length = 0;
    $("mazeDigest").textContent = `digest ${version.digest}`;
    $("mazeDraftState").textContent = `已保存 v${version.version_number}`;
    editorMessage(
      `地图 v${version.version_number} 已保存；历史版本保持不可变。`,
    );
    await refreshMapOptions(version.version_id);
    showNotice(`地图版本 v${version.version_number} 已保存`);
  } catch (error) {
    const message = error instanceof ApiError
      ? error.message
      : error?.message || "地图保存失败";
    editorMessage(message, true);
    showNotice(message, { error: true });
  } finally {
    updateEditability();
  }
}

async function refreshMapOptions(preferredVersion = "") {
  if (!getAppState().authenticated) return;
  const payload = await listMaps();
  const maps = payload.maps || [];
  const versionsByMap = await Promise.all(
    maps.map(async (map) => ({
      map,
      versions: (await listMapVersions(map.map_id)).versions || [],
    })),
  );
  const editorOptions = [
    option("", "新建地图草稿"),
  ];
  const taskOptions = [
    option("", "请选择已保存地图版本"),
  ];
  versionsByMap.forEach(({ map, versions }) => {
    versions.forEach((version) => {
      const label = `${map.name} · v${version.version_number}`;
      editorOptions.push(
        option(
          version.version_id,
          label,
          { mapId: map.map_id },
        ),
      );
      taskOptions.push(option(version.version_id, label));
    });
  });
  $("mazeVersionList").replaceChildren(...editorOptions);
  $("mapVersionInput").replaceChildren(...taskOptions);
  const appState = getAppState();
  const selected = (
    preferredVersion
    || appState.selectedMapVersionId
    || appState.activeTask?.map_version
    || draft.currentVersionId
  );
  if (selected) {
    $("mazeVersionList").value = selected;
    $("mapVersionInput").value = selected;
  }
  $("mapVersionInput").dispatchEvent(new Event("change"));
  draft.mapsLoaded = true;
}

function option(value, label, dataset = {}) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = label;
  Object.assign(element.dataset, dataset);
  return element;
}

async function loadSelectedVersion() {
  const select = $("mazeVersionList");
  const versionId = select.value;
  if (!versionId) {
    draft.currentMapId = "";
    draft.currentVersionId = "";
    $("mazeDraftState").textContent = "未保存草稿";
    $("mazeDigest").textContent = "digest --";
    return;
  }
  try {
    const version = await getMapVersion(versionId);
    loadDefinition(version.definition);
    draft.currentMapId = version.map_id;
    draft.currentVersionId = version.version_id;
    $("mazeDraftState").textContent = `已载入 v${version.version_number}`;
    $("mazeDigest").textContent = `digest ${version.digest}`;
    $("mapVersionInput").value = version.version_id;
    $("mapVersionInput").dispatchEvent(new Event("change"));
    editorMessage("已载入不可变版本；继续编辑会保存为新版本。");
  } catch (error) {
    editorMessage(error.message || "地图版本加载失败", true);
  }
}

function loadDefinition(definition) {
  draft.rows = Number(definition.rows);
  draft.cols = Number(definition.cols);
  draft.walls = new Set(
    (definition.walls || []).map((wall) => {
      if (wall.y1 === wall.y2) {
        return edgeKey("H", wall.y1, Math.min(wall.x1, wall.x2));
      }
      return edgeKey("V", wall.x1, Math.min(wall.y1, wall.y2));
    }),
  );
  draft.start = {
    x: Number(definition.start.x),
    y: Number(definition.start.y),
  };
  draft.goals = (definition.goals || []).map((goal) => ({
    x: Number(goal.x),
    y: Number(goal.y),
  }));
  draft.imageDigest = definition.source_image_digest || null;
  $("mazeRows").value = String(draft.rows);
  $("mazeCols").value = String(draft.cols);
  $("mazeCellWidth").value = String(definition.cell_width_mm);
  $("mazeCellHeight").value = String(definition.cell_height_mm);
  $("mazeWallThickness").value = String(definition.wall_thickness_mm);
  $("mazeWallHeight").value = String(definition.wall_height_mm);
  $("mazeHeading").value = definition.start.heading;
  draft.undo.length = 0;
  draft.redo.length = 0;
  render();
}

function selectImage(event) {
  const [file] = event.target.files || [];
  if (!file) return;
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    editorMessage("底图只支持 PNG、JPEG 或 WebP。", true);
    return;
  }
  if (draft.imageUrl) URL.revokeObjectURL(draft.imageUrl);
  draft.imageFile = file;
  draft.imageUrl = URL.createObjectURL(file);
  draft.imageDigest = null;
  markDirty("底图已载入本地预览；保存版本时会写入 artifact。");
  render();
}

function canEdit() {
  const appState = getAppState();
  return (
    appState.payload?.auth?.control?.role === "controller"
    && Boolean(appState.leaseToken)
  );
}

function updateEditability() {
  const editable = canEdit();
  document
    .querySelectorAll(
      "#mazeEditor button, #mazeEditor input, #mazeEditor select",
    )
    .forEach((control) => {
      if (["mazeUndo", "mazeRedo"].includes(control.id)) return;
      if (control.id === "mazeVersionList") {
        control.disabled = !getAppState().authenticated;
      } else {
        control.disabled = !editable;
      }
    });
  $("mazeUndo").disabled = !editable || !draft.undo.length;
  $("mazeRedo").disabled = !editable || !draft.redo.length;
  $("mazeCanvas").classList.toggle("is-readonly", !editable);
}

function markDirty(message = "草稿有未保存更改") {
  $("mazeDraftState").textContent = "未保存更改";
  $("mazeDigest").textContent = "digest 待校验";
  editorMessage(message);
}

function editorMessage(message, error = false) {
  $("mazeEditorMessage").textContent = message;
  $("mazeEditorMessage").classList.toggle("is-error", error);
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => {
    element.setAttribute(key, String(value));
  });
  return element;
}

function render() {
  const canvas = $("mazeCanvas");
  canvas.setAttribute("viewBox", `0 0 ${draft.cols} ${draft.rows}`);
  const nodes = [];
  if (draft.imageUrl) {
    nodes.push(
      svgElement("image", {
        class: "source-image",
        href: draft.imageUrl,
        x: 0,
        y: 0,
        width: draft.cols,
        height: draft.rows,
        opacity: $("mazeImageOpacity").value,
        preserveAspectRatio: "none",
      }),
    );
  }
  for (let x = 0; x <= draft.cols; x += 1) {
    nodes.push(
      svgElement("line", {
        class: "grid-line",
        x1: x,
        y1: 0,
        x2: x,
        y2: draft.rows,
      }),
    );
  }
  for (let y = 0; y <= draft.rows; y += 1) {
    nodes.push(
      svgElement("line", {
        class: "grid-line",
        x1: 0,
        y1: y,
        x2: draft.cols,
        y2: y,
      }),
    );
  }
  [...draft.walls].sort().forEach((key) => {
    const wall = parseEdge(key);
    nodes.push(
      svgElement("line", {
        class: "wall-line",
        ...wall,
      }),
    );
  });
  nodes.push(...renderGoals(), ...renderStart(), ...renderCalibration());
  canvas.replaceChildren(...nodes);
  updateHistoryButtons();
  updateEditability();
}

function renderGoals() {
  return draft.goals.map((goal) =>
    svgElement("circle", {
      class: "goal-cell",
      cx: goal.x + 0.5,
      cy: goal.y + 0.5,
      r: 0.23,
    }),
  );
}

function renderStart() {
  const centerX = draft.start.x + 0.5;
  const centerY = draft.start.y + 0.5;
  const rotation = { N: 0, E: 90, S: 180, W: 270 }[
    $("mazeHeading").value
  ] || 0;
  return [
    svgElement("rect", {
      class: "start-cell",
      x: draft.start.x + 0.16,
      y: draft.start.y + 0.16,
      width: 0.68,
      height: 0.68,
      rx: 0.08,
    }),
    svgElement("path", {
      class: "start-arrow",
      d: `M ${centerX} ${centerY - 0.28} L ${centerX - 0.15} ${centerY + 0.12} L ${centerX} ${centerY + 0.04} L ${centerX + 0.15} ${centerY + 0.12} Z`,
      transform: `rotate(${rotation} ${centerX} ${centerY})`,
    }),
  ];
}

function renderCalibration() {
  return draft.calibrationPoints.map((point) =>
    svgElement("circle", {
      class: "calibration-point",
      cx: point.x,
      cy: point.y,
      r: 0.1,
    }),
  );
}

function bind() {
  $("mazeToolDraw").addEventListener("click", () => setTool("draw"));
  $("mazeToolErase").addEventListener("click", () => setTool("erase"));
  $("mazeSetStart").addEventListener("click", () => setTool("start"));
  $("mazeSetGoal").addEventListener("click", () => setTool("goal"));
  $("mazeCalibrate").addEventListener("click", () => setTool("calibrate"));
  $("mazeUndo").addEventListener("click", undo);
  $("mazeRedo").addEventListener("click", redo);
  $("mazeApplyGrid").addEventListener("click", applyGridSize);
  $("mazeSaveVersion").addEventListener("click", save);
  $("mazeVersionList").addEventListener("change", loadSelectedVersion);
  $("mazeImageInput").addEventListener("change", selectImage);
  $("mazeImageOpacity").addEventListener("input", render);
  $("mazeHeading").addEventListener("change", () => {
    markDirty("初始车头方向已更新");
    render();
  });
  $("mazeCanvas").addEventListener("pointerdown", handlePointerDown);
  $("mazeCanvas").addEventListener("pointermove", handlePointerMove);
  $("mazeCanvas").addEventListener("pointerup", handlePointerUp);
  $("mazeCanvas").addEventListener("pointercancel", handlePointerUp);
}

export function initializeMazeEditor() {
  if (draft.initialized) return;
  draft.initialized = true;
  draft.walls = boundaryWalls(draft.rows, draft.cols);
  bind();
  render();
  subscribe((appState) => {
    updateEditability();
    if (appState.authenticated && !draft.mapsLoaded) {
      refreshMapOptions().catch((error) => {
        editorMessage(error.message || "地图版本加载失败", true);
      });
    }
    if (!appState.authenticated) {
      draft.mapsLoaded = false;
    }
  });
  if (getAppState().authenticated) {
    refreshMapOptions().catch(() => {});
  }
}
