// Types mirroring the Python contracts in `src/flow/schema.py` and the desktop
// API. Kept hand-written rather than generated: the canvas depends on `path` and
// `target` being *required* on the node type so a designer cannot save a graph
// whose location strategy is only decided at run time, and a generator would
// happily type them optional.

/** The dual-path declaration chosen when the flow is configured. */
export type LocatePath = "element" | "element_strict" | "vision" | "auto";

export const LOCATE_PATHS: readonly LocatePath[] = [
  "element",
  "element_strict",
  "vision",
  "auto",
] as const;

export const PATH_LABELS: Record<LocatePath, string> = {
  element: "元素库",
  element_strict: "元素库(严格)",
  vision: "多模态",
  auto: "自动(元素优先)",
};

export const PATH_HINTS: Record<LocatePath, string> = {
  element: "从元素库解析，失败则失败",
  element_strict: "只走元素库，绝不调用视觉模型",
  vision: "截图交给视觉模型定位，与元素库无关",
  auto: "元素库优先，缺失时降级到视觉模型（会付费）",
};

export interface Position {
  x: number;
  y: number;
}

export interface Retry {
  max: number;
  delay: number;
}

export type OnError = "fail" | "branch" | "ignore";

export interface FlowNode {
  id: string;
  type: string;
  name: string;
  position: Position;
  params: Record<string, unknown>;
  retry: Retry;
  timeout: number | null;
  on_error: OnError;
  outputs: string[];
  disabled: boolean;
  /** Design-time location strategy. `null` means "inherit the flow default". */
  path: LocatePath | null;
  target: string | null;
}

export interface FlowEdge {
  id: string;
  source: string;
  source_port: string;
  target: string;
  condition: string | null;
  label: string;
}

export interface FlowGraph {
  version: 1;
  entry: string;
  default_path?: LocatePath | null;
  description?: string;
  variables: Record<string, unknown>;
  nodes: FlowNode[];
  edges: FlowEdge[];
}

export interface ValidationIssue {
  severity: "error" | "warning";
  code: string;
  message: string;
  node_id: string | null;
  edge_id: string | null;
}

export interface Flow {
  id: string;
  name: string;
  description: string;
  version: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  graph: FlowGraph;
  issues?: ValidationIssue[];
}

export interface FlowSummary {
  id: string;
  name: string;
  description: string;
  version: number;
  is_active: number;
  created_at: string;
  updated_at: string;
  run_count: number;
  last_status: string | null;
  last_run_at: number | null;
  running?: boolean;
}

export type ParamKind = "text" | "textarea" | "number" | "select" | "bool" | "variable";

export interface ParamSpec {
  name: string;
  kind: ParamKind;
  label: string;
  default: unknown;
  choices: unknown[];
  help: string;
  required: boolean;
}

export interface NodeSpec {
  type: string;
  label: string;
  category: string;
  params: ParamSpec[];
  outputs: string[];
  doc: string;
  requires: string[];
  singleton: boolean;
  /** Whether the canvas renders a path selector on the node body itself. */
  path_aware: boolean;
  path_choices: LocatePath[];
}

export interface NodeCategory {
  category: string;
  nodes: NodeSpec[];
}

export interface NodeCatalogue {
  categories: NodeCategory[];
  path_choices: LocatePath[];
  total: number;
}

export type RunStatus = "running" | "ok" | "error" | "aborted";

export interface Run {
  id: string;
  flow_id: string;
  flow_name?: string;
  trigger_type: string;
  trigger_ref: string;
  status: RunStatus;
  steps: number;
  error: string | null;
  failed_node: string | null;
  variables: Record<string, unknown>;
  scope: Record<string, unknown>;
  started_at: number;
  ended_at: number | null;
  span_count?: number;
}

export type SpanStatus = "running" | "ok" | "error" | "skipped";

export interface Span {
  id: number;
  run_id: string;
  node_id: string;
  node_type: string;
  name: string;
  attempt: number;
  status: SpanStatus;
  started_at: number;
  ended_at: number | null;
  duration_ms: number | null;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  error: string | null;
  screenshot_path: string | null;
}

/** One SSE event from `/api/stream`. */
export type TraceEvent =
  | ({ type: "span"; phase: "started" | "finished" } & Partial<Span>)
  | { type: "run.started"; run_id: string; flow_id: string; flow_name?: string }
  | { type: "run.finished"; run_id: string; flow_id: string; status: string; steps?: number; error?: string | null; duration_ms?: number }
  | { type: "dropped"; count: number };

export interface Element {
  id: string;
  name: string;
  kind: "rect" | "ocr" | "image" | "web";
  rect: {
    x: number;
    y: number;
    width: number;
    height: number;
    relative_to: "window" | "screen";
  };
  ocr_text: string;
  image_path: string | null;
  flow_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Located {
  resolved: boolean;
  x?: number;
  y?: number;
  source?: "element" | "vision";
  confidence?: number;
  label?: string;
  element_name?: string | null;
  mode?: string;
  error?: string;
  detail?: Record<string, unknown>;
}

export interface Target {
  name: string;
  kind: "desktop" | "web";
  match: string;
  mode: LocatePath;
  window_titles: string[];
  url_pattern: string;
  description: string;
}

export type PermissionStatus = "granted" | "denied" | "not_determined" | "unavailable";

export interface Permission {
  key: string;
  status: PermissionStatus;
  granted: boolean;
  title: string;
  detail: string;
  gates: string;
  pane_url: string;
  fix: string;
}

export interface PermissionReport {
  ok: boolean;
  deep: boolean;
  missing: string[];
  wechat_running: string;
  permissions: Permission[];
  summary: string;
}

export interface Schedule {
  id: string;
  name: string;
  flow_id: string;
  flow_name?: string;
  cron: string;
  enabled: number;
  last_run_at: string | null;
  last_status: string | null;
  last_run_id: string | null;
  created_at: string;
  cron_preview?: string[];
}

/**
 * A configured LLM gateway. `api_key` coming back from the API is always a
 * mask; `api_key_set` says whether one is stored, so the form can show
 * "已配置" without ever holding the real key.
 */
export interface LLMProvider {
  id: string;
  name: string;
  base_url: string;
  api_key: string;
  api_key_set: boolean;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  timeout: number | null;
  is_default: number;
  enabled: number;
  note: string;
  last_ok_at: string | null;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export interface CronPreview {
  valid: boolean;
  error?: string;
  raw?: string;
  fields?: string;
  next_runs?: string[];
}

export interface BotStatus {
  running: boolean;
  pid: number | null;
  last_tick: string | null;
  model: string;
  wechat_status: string;
}

export interface DashboardSummary {
  date: string;
  ticks: number;
  replies: number;
  avg_score: number;
  skipped: number;
  skip_rate: number;
}

export interface WebhookInfo {
  configured: boolean;
  path: string | null;
  hint: string;
}

export interface ValidateResponse {
  ok: boolean;
  issues: ValidationIssue[];
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
}

// ── recording & picking ───────────────────────────────────────────────────
//
// A recording is a list of what the user did, not a flow. The client keeps the
// raw actions and the generated graph side by side so the panel can show the
// difference between "captured" and "committed".

export interface RecordedAction {
  index: number;
  kind: "click" | "double_click" | "scroll" | "type_keys" | "hotkey";
  at: number;
  x: number;
  y: number;
  button: number;
  clicks: number;
  text: string;
  scroll_delta: number;
  modifiers: string[];
  app: string;
  label: string;
  node_type: string;
  params: Record<string, unknown>;
}

export interface RecordStatus {
  recording: boolean;
  error: string;
  count: number;
}

export interface RecordStopResult {
  recording: boolean;
  error: string;
  count: number;
  actions: RecordedAction[];
  graph: FlowGraph;
  warnings: string[];
}

export interface RecordCommitResult {
  flow_id: string;
  name: string;
  action_count: number;
  warnings: string[];
}

/** Which of the three pick strategies was used, and what that means. */
export type PickStrategy = "ocr_anchor" | "image_template" | "fixed_rect";

export interface PickResult {
  element: Element;
  strategy: PickStrategy;
  advice: string;
}

export interface PickScreenshot {
  image_path: string;
  scale_factor: number;
  window_origin: [number, number, number, number] | null;
}

export interface ElementTemplate {
  name: string;
  path: string;
  size: number;
  modified: number;
}
