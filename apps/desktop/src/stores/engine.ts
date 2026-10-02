// Backend liveness: bot process, dashboard metrics, log tail, permissions.
//
// Kept separate from the flow store because these are polled on a timer even
// when the user is on a page that has nothing to do with flows, and a single
// store would force the canvas to re-render every 3 seconds for no reason.

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, ApiError } from "../api/client";
import type { BotStatus, DashboardSummary, PermissionReport } from "../types";

const POLL_MS = 3000;

export const useEngineStore = defineStore("engine", () => {
  /**
   * Tri-state on purpose.
   *
   * `null` is "not measured yet", which is a different fact from `false`
   * ("we asked and it was not there"). Collapsing the two is how a page opened
   * directly at a sub-route showed "LOCAL API 离线" for the length of one round
   * trip while the API was answering 200 — the indicator reported a
   * measurement nobody had made.
   */
  const online = ref<boolean | null>(null);
  const status = ref<BotStatus | null>(null);
  const summary = ref<DashboardSummary | null>(null);
  const logs = ref<string[]>([]);
  const permissions = ref<PermissionReport | null>(null);
  const lastError = ref("");
  const lastUpdated = ref("");
  const busy = ref(false);

  let timer: number | null = null;
  let inFlight = false;

  const running = computed(() => status.value?.running === true);

  const wechatLabel = computed(() => {
    const value = status.value?.wechat_status;
    if (!value) return "未知";
    if (value === "ready") return "已就绪";
    if (value === "not_running") return "未运行";
    if (value === "check_unavailable") return "无法检测";
    if (value === "check_failed") return "检测失败";
    if (value.startsWith("window_too_small:")) {
      return `窗口过小 ${value.split(":")[1]}`;
    }
    return value;
  });

  const wechatReady = computed(() => status.value?.wechat_status === "ready");

  async function refresh(): Promise<void> {
    if (inFlight) return;
    inFlight = true;
    // Settled, not all: liveness is decided by /api/status alone. An earlier
    // version required all three to succeed, so an unreadable bot log or a
    // cases.db that was not readable made the header declare the whole local
    // service dead while it was serving. Each panel shows its own failure.
    const [nextStatus, nextLogs, nextSummary] = await Promise.allSettled([
      api.status(),
      api.logs(200),
      api.summary(),
    ]);

    if (nextStatus.status === "fulfilled") {
      status.value = nextStatus.value;
      online.value = true;
      lastError.value = "";
      lastUpdated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } else {
      online.value = false;
      lastError.value =
        nextStatus.reason instanceof ApiError
          ? nextStatus.reason.message
          : String(nextStatus.reason);
    }

    if (nextLogs.status === "fulfilled") logs.value = nextLogs.value.logs;
    if (nextSummary.status === "fulfilled") summary.value = nextSummary.value;
    inFlight = false;
  }

  /** Permissions are polled shallow: `deep=true` raises a system consent dialog
   *  and must only be triggered by the permission page itself. */
  async function refreshPermissions(deep = false): Promise<void> {
    try {
      permissions.value = await api.permissions(deep);
    } catch (error) {
      lastError.value = error instanceof ApiError ? error.message : String(error);
    }
  }

  async function startBot(): Promise<void> {
    busy.value = true;
    try {
      await api.startBot(false);
      await refresh();
    } catch (error) {
      lastError.value = error instanceof ApiError ? error.message : String(error);
      throw error;
    } finally {
      busy.value = false;
    }
  }

  async function stopBot(): Promise<void> {
    busy.value = true;
    try {
      await api.stopBot();
      await refresh();
    } finally {
      busy.value = false;
    }
  }

  function startPolling(): void {
    if (timer !== null) return;
    void refresh();
    void refreshPermissions(false);
    timer = window.setInterval(() => void refresh(), POLL_MS);
  }

  function stopPolling(): void {
    if (timer === null) return;
    window.clearInterval(timer);
    timer = null;
  }

  return {
    online,
    status,
    summary,
    logs,
    permissions,
    lastError,
    lastUpdated,
    busy,
    running,
    wechatLabel,
    wechatReady,
    refresh,
    refreshPermissions,
    startBot,
    stopBot,
    startPolling,
    stopPolling,
  };
});
