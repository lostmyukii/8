import {
  ApiError,
  claimControl,
  createTask,
  emergencyStop,
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
  setSelectedMode,
} from "./state.js";
import { showNotice } from "./render.js";

const $ = (id) => document.getElementById(id);
let operationBusy = false;
let leaseHeartbeatTimer = null;

function coerceValue(raw, valueType) {
  if (valueType === "number") {
    return raw.includes(".") ? Number.parseFloat(raw) : Number.parseInt(raw, 10);
  }
  if (raw === "true") return true;
  if (raw === "false") return false;
  return raw;
}

function taskDefinition() {
  const appState = getAppState();
  return {
    mode: appState.selectedMode,
    map_version: $("mapVersionInput").value.trim(),
    param_version: $("paramVersionInput").value.trim(),
    goal: {
      type: "cell",
      cell: [
        Number.parseInt($("goalX").value, 10),
        Number.parseInt($("goalY").value, 10),
      ],
    },
    max_steps: 500,
  };
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
        if (!task || task.mode !== getAppState().selectedMode) {
          task = await createTask(taskDefinition());
          setActiveTask(task.task_id);
        }
        await taskOperation(task.task_id, "preflight");
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
      const task = getAppState().activeTask;
      if (!task) throw new Error("请先执行预检并重置");
      await taskOperation(task.task_id, operation);
      await refreshState();
    },
    { success },
  ).catch(() => {});
}
