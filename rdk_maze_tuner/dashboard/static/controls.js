import {
  ApiError,
  claimControl,
  createTask,
  debugStep,
  emergencyStop,
  getMapVersion,
  heartbeatControl,
  login,
  logout,
  manualAction,
  releaseControl,
  taskOperation,
  updateAutoTune,
  updateParam,
} from "./api.js";
import {
  clearAuthSession,
  getAppState,
  setActiveTask,
  setAuthSession,
  setLeaseToken,
  setMapVersionDetail,
  setMapVersionError,
  setMapVersionLoading,
  setSelectedPhysicalProfile,
  setSelectedMode,
} from "./state.js";
import { showNotice } from "./render.js";

const $ = (id) => document.getElementById(id);
let operationBusy = false;
let leaseHeartbeatTimer = null;
let debugPreview = null;

function coerceValue(raw, valueType) {
  if (valueType === "number") {
    return raw.includes(".") ? Number.parseFloat(raw) : Number.parseInt(raw, 10);
  }
  if (raw === "true") return true;
  if (raw === "false") return false;
  return raw;
}

function taskDefinition() {
  const appState = assertAutomaticMapReady();
  const definition = {
    run_kind: "auto_to_map_goal",
    mode: appState.selectedMode,
    map_version: appState.selectedMapVersionId,
    param_version: $("paramVersionInput").value.trim(),
    max_steps: 500,
  };
  if (appState.selectedMode === "simulation") {
    definition.physical_profile_id =
      appState.selectedPhysicalProfileId || "normal-v1";
  }
  return definition;
}

function taskDefinitionChanged(task, definition) {
  const physicalProfileChanged =
    definition.mode === "simulation"
    && task.physical_profile_id !== definition.physical_profile_id;
  return (
    task.run_kind !== definition.run_kind
    || task.mode !== definition.mode
    || task.map_version !== definition.map_version
    || task.param_version !== definition.param_version
    || physicalProfileChanged
  );
}

function debugStepDefinition() {
  const appState = assertAutomaticMapReady();
  const x = Number($("debugGoalX").value);
  const y = Number($("debugGoalY").value);
  if (!Number.isInteger(x) || !Number.isInteger(y)) {
    throw new Error("调试坐标必须是整数格坐标");
  }
  return {
    mapVersion: appState.selectedMapVersionId,
    targetCell: [x, y],
    signature: `${appState.selectedMapVersionId}:${x}:${y}`,
  };
}

function resetDebugPreview() {
  debugPreview = null;
  const button = $("debugCoordinateButton");
  if (button) {
    button.dataset.confirmed = "false";
    button.textContent = "预览下一动作";
  }
  const notice = $("manualGoalNotice");
  if (notice) {
    notice.textContent =
      "单步调试不会改变自动终点，也不会触发自动完成。";
  }
}

function assertAutomaticMapReady() {
  const appState = getAppState();
  const selected = $("mapVersionInput").value.trim();
  const ready = (
    selected
    && appState.selectedMapVersionId === selected
    && appState.mapVersionStatus === "ready"
    && appState.mapVersionDetail?.version_id === selected
    && appState.mapGoal?.source_map_version === selected
  );
  if (!ready) {
    throw new Error(
      appState.mapVersionError
      || "请等待地图定义、摘要和自动终点加载完成",
    );
  }
  return appState;
}

async function loadAutomaticMapVersion(versionId) {
  const normalized = String(versionId || "").trim();
  setMapVersionLoading(normalized);
  if (!normalized) return;
  try {
    const version = await getMapVersion(normalized);
    setMapVersionDetail(version);
  } catch (error) {
    setMapVersionError(normalized, error);
    showNotice(errorMessage(error), { error: true, timeout: 5000 });
  }
}

function errorMessage(error) {
  if (error instanceof ApiError) return error.message;
  return error?.message || "操作失败";
}

async function guardedOperation(callback, { success } = {}) {
  if (operationBusy) return null;
  operationBusy = true;
  try {
    const result = await callback();
    if (success) showNotice(success);
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearAuthSession();
    }
    showNotice(errorMessage(error), { error: true, timeout: 5000 });
    throw error;
  } finally {
    operationBusy = false;
  }
}

function startLeaseHeartbeat(refreshState) {
  if (leaseHeartbeatTimer) window.clearInterval(leaseHeartbeatTimer);
  leaseHeartbeatTimer = window.setInterval(async () => {
    if (!getAppState().leaseToken) return;
    try {
      await heartbeatControl();
      await refreshState();
    } catch (error) {
      setLeaseToken("");
      showNotice(`控制权续租失败：${errorMessage(error)}`, { error: true });
      await refreshState().catch(() => {});
    }
  }, 4000);
}

export function bindControls({ refreshState, onAuthenticated, onLogout }) {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("loginError").textContent = "";
    try {
      const credentials = await login(
        $("username").value.trim(),
        $("password").value,
      );
      setAuthSession({
        csrfToken: credentials.csrf_token,
        user: credentials.user,
      });
      $("password").value = "";
      await refreshState();
      startLeaseHeartbeat(refreshState);
      onAuthenticated?.();
    } catch (error) {
      $("loginError").textContent = errorMessage(error);
    }
  });

  $("logoutButton").addEventListener("click", async () => {
    try {
      await logout();
    } catch (error) {
      showNotice(errorMessage(error), { error: true });
    } finally {
      if (leaseHeartbeatTimer) window.clearInterval(leaseHeartbeatTimer);
      clearAuthSession();
      onLogout?.();
    }
  });

  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      setSelectedMode(button.dataset.mode);
    });
  });

  $("physicalProfileInput").addEventListener("change", (event) => {
    setSelectedPhysicalProfile(event.target.value);
  });
  $("mapVersionInput").addEventListener("change", (event) => {
    resetDebugPreview();
    loadAutomaticMapVersion(event.target.value);
  });
  $("debugGoalX").addEventListener("input", resetDebugPreview);
  $("debugGoalY").addEventListener("input", resetDebugPreview);

  $("claimControlButton").addEventListener("click", async () => {
    if (!getAppState().csrfToken) {
      showNotice("当前标签页缺少 CSRF 凭据，请重新登录后取得控制权。", {
        error: true,
      });
      return;
    }
    await guardedOperation(
      async () => {
        const grant = await claimControl();
        setLeaseToken(grant.lease_token);
        startLeaseHeartbeat(refreshState);
        await refreshState();
      },
      { success: "已取得控制权" },
    ).catch(() => {});
  });

  $("releaseControlButton").addEventListener("click", async () => {
    await guardedOperation(
      async () => {
        await releaseControl();
        setLeaseToken("");
        await refreshState();
      },
      { success: "已释放控制权" },
    ).catch(() => {});
  });

  $("taskResetButton").addEventListener("click", async () => {
    await guardedOperation(
      async () => {
        let task = getAppState().activeTask;
        const definition = taskDefinition();
        if (!task || taskDefinitionChanged(task, definition)) {
          task = await createTask(definition);
          setActiveTask(task.task_id);
        }
        const preflight = await taskOperation(task.task_id, "preflight");
        if (preflight?.preflight?.ok !== true) {
          await refreshState();
          throw new Error(
            preflight?.preflight?.message || "物理安全预检未通过",
          );
        }
        await taskOperation(task.task_id, "reset");
        await refreshState();
      },
      { success: "预检通过，任务已重置" },
    ).catch(() => {});
  });

  $("taskStartButton").addEventListener("click", async () => {
    await runTaskOperation("start", "自动探索已开始", refreshState);
  });

  $("taskPauseButton").addEventListener("click", async () => {
    await runTaskOperation("pause", "正在安全暂停", refreshState);
  });

  $("stopButton").addEventListener("click", async () => {
    await runTaskOperation("stop", "任务已停止", refreshState);
  });

  $("estopButton").addEventListener("click", async () => {
    await guardedOperation(
      async () => {
        await emergencyStop("dashboard shared emergency stop");
        await refreshState();
      },
      { success: "急停已触发" },
    ).catch(() => {});
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      await guardedOperation(async () => {
        await manualAction(button.dataset.action);
        await refreshState();
      }).catch(() => {});
    });
  });

  $("debugCoordinateButton").addEventListener("click", async () => {
    await guardedOperation(async () => {
      const definition = debugStepDefinition();
      const execute = debugPreview?.signature === definition.signature;
      const response = await debugStep(
        definition.mapVersion,
        definition.targetCell,
        execute,
      );
      if (execute) {
        resetDebugPreview();
        await refreshState();
        const outcome = response?.result?.outcome || "unknown";
        showNotice(`单步调试已执行：${outcome}`);
        return;
      }
      debugPreview = {
        signature: definition.signature,
        action: response.next_action,
      };
      const action = response.next_action?.name || "无需动作";
      $("debugCoordinateButton").dataset.confirmed = "true";
      $("debugCoordinateButton").textContent = `执行这一步：${action}`;
      $("manualGoalNotice").textContent =
        `已预览 ${action}；目标未变时再次点击才会执行。`;
    }).catch(() => {});
  });

  $("autoTuneToggle").addEventListener("change", async (event) => {
    await guardedOperation(async () => {
      await updateAutoTune(Boolean(event.target.checked));
      await refreshState();
    }).catch(() => {});
  });

  $("paramTable").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-save-param]");
    if (!button) return;
    const input = button.closest("tr")?.querySelector(".param-input");
    if (!input) return;
    await guardedOperation(
      async () => {
        await updateParam(
          button.dataset.saveParam,
          coerceValue(input.value, input.dataset.valueType),
        );
        await refreshState();
      },
      { success: `${button.dataset.saveParam} 已校验并下发` },
    ).catch(() => {});
  });

  if (getAppState().leaseToken) startLeaseHeartbeat(refreshState);
}

async function runTaskOperation(operation, success, refreshState) {
  await guardedOperation(
    async () => {
      if (operation === "start") assertAutomaticMapReady();
      const task = getAppState().activeTask;
      if (!task) throw new Error("请先执行预检并重置");
      await taskOperation(task.task_id, operation);
      await refreshState();
    },
    { success },
  ).catch(() => {});
}
