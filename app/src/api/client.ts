// Typed client for the desktop API on 127.0.0.1:8767.
//
// The Tauri webview can reach the loopback service directly, so this is plain
// fetch rather than a Tauri invoke wrapper. The one exception is starting the
// backend itself, which has to go through Rust because the webview may be alive
// before the API is.

import type {
  BotStatus,
  CronPreview,
  DashboardSummary,
  Element,
  ElementTemplate,
  Flow,
  FlowGraph,
  FlowSummary,
  Located,
  NodeCatalogue,
  PermissionReport,
  PickResult,
  PickScreenshot,
  RecordCommitResult,
  RecordedAction,
  RecordStatus,
  RecordStopResult,
  Run,
  Schedule,
  Span,
  Target,
  ValidateResponse,
  WebhookInfo,
} from "../types";

export const API_BASE = "http://127.0.0.1:8767";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch (cause) {
    // A dead backend is the single most common failure in this app, and the
    // message a user sees should say so rather than surfacing "Failed to fetch".
    throw new ApiError("无法连接本地 Python 服务（127.0.0.1:8767）", 0, cause);
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown })?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : ((detail as { message?: string })?.message ?? `请求失败 (${response.status})`);
    throw new ApiError(message, response.status, detail);
  }
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  // ── process & dashboard ──
  health: () => request<{ status: string }>("/health"),
  status: () => request<BotStatus>("/status"),
  summary: () => request<DashboardSummary>("/dashboard/summary"),
  logs: (lines = 200) => request<{ logs: string[] }>(`/logs?lines=${lines}`),
  startBot: (skipPreflight = false) => post<{ status: string; pid: number }>(`/bot/start?skip_preflight=${skipPreflight}`),
  stopBot: () => post<{ status: string }>("/bot/stop"),

  // ── palette ──
  nodes: () => request<NodeCatalogue>("/flow/nodes"),
  targets: () => request<{ targets: Target[] }>("/flow/targets"),
  saveTarget: (target: Partial<Target>) => post<Target>("/flow/targets", target),

  // ── flows ──
  flows: () => request<{ flows: FlowSummary[] }>("/flows"),
  flow: (id: string) => request<Flow>(`/flows/${id}`),
  createFlow: (payload: { name: string; description?: string; graph?: FlowGraph }) =>
    post<Flow>("/flows", payload),
  saveFlow: (id: string, payload: { name?: string; description?: string; graph: FlowGraph; strict?: boolean }) =>
    request<Flow>(`/flows/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteFlow: (id: string) => request<{ deleted: string }>(`/flows/${id}`, { method: "DELETE" }),
  duplicateFlow: (id: string) => post<Flow>(`/flows/${id}/duplicate`),
  /** Validate a candidate graph without saving — called on every canvas edit. */
  validate: (id: string, graph: FlowGraph) => post<ValidateResponse>(`/flows/${id}/validate`, { graph }),

  // ── runs ──
  run: (id: string, payload: { dry_run?: boolean; force?: boolean; variables?: Record<string, unknown> } = {}) =>
    post<{ accepted: boolean; run_id: string }>(`/flows/${id}/run`, payload),
  runs: (params: { flow_id?: string; status?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.flow_id) query.set("flow_id", params.flow_id);
    if (params.status) query.set("status", params.status);
    query.set("limit", String(params.limit ?? 50));
    return request<{ runs: Run[] }>(`/runs?${query}`);
  },
  run_detail: (runId: string) => request<Run>(`/runs/${runId}`),
  spans: (runId: string) => request<{ spans: Span[] }>(`/runs/${runId}/spans`),
  abortRun: (runId: string) => post<{ aborted: boolean }>(`/runs/${runId}/abort`),
  abortAll: () => post<{ aborted: string[] }>("/runs/abort-all"),

  // ── elements ──
  elements: (flowId?: string) =>
    request<{ elements: Element[] }>(`/elements${flowId ? `?flow_id=${flowId}` : ""}`),
  saveElement: (payload: Partial<Element> & { name: string }) => post<Element>("/elements", payload),
  deleteElement: (id: string) => request<{ deleted: string }>(`/elements/${id}`, { method: "DELETE" }),
  /** Dry-resolve an element: reports the point without clicking. */
  resolveElement: (name: string) => post<Located>(`/elements/${encodeURIComponent(name)}/resolve`),

  // ── permissions ──
  permissions: (deep = false) => request<PermissionReport>(`/permissions?deep=${deep}`),
  promptPermission: (key: string) => post<{ key: string; prompted: boolean; granted: boolean }>(`/permissions/${key}/prompt`),
  openPermissionPane: (key: string) => post<{ key: string; opened: boolean }>(`/permissions/${key}/open`),
  resetVisionClient: () => post<{ reset: boolean }>("/permissions/vision/reset"),

  // ── schedules ──
  schedules: () => request<{ schedules: Schedule[]; running: boolean; stats: Record<string, number | string> }>("/schedules"),
  saveSchedule: (payload: Partial<Schedule>) => post<Schedule>("/schedules", payload),
  deleteSchedule: (id: string) => request<{ deleted: string }>(`/schedules/${id}`, { method: "DELETE" }),
  previewCron: (cron: string) => post<CronPreview>("/schedules/cron/preview", { cron }),

  webhookInfo: () => request<WebhookInfo>("/webhook/info"),

  // ── recording ──
  //
  // The recorder watches the user's own input, so its state is process-wide and
  // the status endpoint is polled rather than pushed: there is no run to hang an
  // SSE subscription off, and a dead monitor thread has to be visible rather than
  // showing a spinner forever.
  recordStatus: () => request<RecordStatus>("/record/status"),
  recordStart: () => post<{ recording: boolean; started_at: number }>("/record/start"),
  recordActions: () => request<{ recording: boolean; count: number; actions: RecordedAction[] }>("/record/actions"),
  recordStop: (payload: { name?: string; description?: string } = {}) =>
    post<RecordStopResult>("/record/stop", payload),
  recordDiscard: () => post<{ recording: boolean; count: number }>("/record/discard"),
  recordCommit: (payload: { name: string; description?: string }) =>
    post<RecordCommitResult>("/record/commit", payload),

  // ── picking ──
  pickScreenshot: (target = "wechat") =>
    request<PickScreenshot>(`/pick/screenshot?target=${encodeURIComponent(target)}`),
  pick: (payload: {
    name: string;
    image_path: string;
    x: number;
    y: number;
    width: number;
    height: number;
    flow_id?: string | null;
    save_template?: boolean;
    min_confidence?: number;
  }) => post<PickResult>("/pick", payload),
  pickTemplates: () => request<{ templates: ElementTemplate[] }>("/pick/templates"),
};

/**
 * Subscribe to the live trace stream.
 *
 * `EventSource` reconnects on its own, which is what we want — but it silently
 * restarts from "now", so a reconnect mid-run loses the earlier spans. The
 * caller re-fetches `/runs/{id}/spans` on `onReconnect` to fill the gap.
 */
export function subscribeTrace(
  runId: string | null,
  onEvent: (event: unknown) => void,
  onReconnect?: () => void,
): () => void {
  const url = runId ? `${API_BASE}/api/stream?run_id=${encodeURIComponent(runId)}` : `${API_BASE}/api/stream`;
  let source: EventSource | null = null;
  let closed = false;
  let hadError = false;

  const open = () => {
    if (closed) return;
    source = new EventSource(url);
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data));
      } catch {
        /* a malformed frame must not kill the stream */
      }
    };
    source.onerror = () => {
      if (closed) return;
      source?.close();
      if (hadError && onReconnect) onReconnect();
      hadError = true;
      window.setTimeout(open, 1500);
    };
  };

  open();
  return () => {
    closed = true;
    source?.close();
  };
}
