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

export interface SchedulerLease {
  path: string;
  held: boolean;
  pid: number | null;
  host: string | null;
  port: number | null;
  since: number | null;
  since_iso: string | null;
}

export interface SchedulerStatus {
  running: boolean;
  /** True when another process on the same database owns the schedule loop.
   *  The API is up and schedules are enabled, but nothing fires from here —
   *  which looks identical to a broken schedule unless the UI says so. */
  standby: boolean;
  standby_reason: string;
  lease: SchedulerLease;
  stats: Record<string, number | string>;
  tick_seconds: number;
}

export interface ScheduleStatus extends SchedulerStatus {
  schedules: Schedule[];
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

/* ── cases domain ──────────────────────────────────────────────────────────
 *
 * Mirrors `services/company_api/cases_api.py`, the router that owns `data/cases.db`.
 * Kept separate from the flow types above on purpose: the flow engine never
 * reads cases.db, and a type that crosses that boundary is a coupling bug
 * waiting to happen.
 */

/**
 * Every cases response carries these.
 *
 * The router answers **HTTP 200 with a full, healthy-looking payload** when
 * cases.db is missing or its schema has drifted, tagging it `degraded: true`
 * plus the sqlite reason. An unreadable store and a genuinely empty one are
 * the same shape otherwise, and a code-audit list in that state renders as a
 * perfect worklist with every issue reset to `pending` — the project's worst
 * bug class, arrived at from the database side. Read these before trusting a
 * "successful" response.
 */
export interface DegradedEnvelope {
  degraded?: boolean;
  degraded_reason?: string;
}

export interface CasesSummary extends DegradedEnvelope {
  date: string;
  ticks: number;
  replies: number;
  avg_score: number;
  skipped: number;
  skip_rate: number;
}

/** One row of the tick log. The list endpoint returns the subset in TICK_LIST_COLUMNS. */
export interface TickRow {
  id: number;
  session_id: string | null;
  tick_id: number | null;
  chat_name: string | null;
  messages_count: number | null;
  new_messages_count: number | null;
  should_reply: number | null;
  send_success: number | null;
  skip_reason: string | null;
  judge_score: number | null;
  human_is_badcase: number | null;
  human_badcase_type: string | null;
  replies_sent_json: string | null;
  raw_response: string | null;
  duration_ms: number | null;
  created_at: string | null;
}

export type TickFilter = "all" | "replied" | "skipped";

export interface TickListResponse extends DegradedEnvelope {
  total: number;
  page: number;
  size: number;
  filter: TickFilter;
  rows: TickRow[];
}

/** The full `SELECT *` row behind the tick detail page. */
export interface TickDetail extends TickRow, DegradedEnvelope {
  screenshot_path: string | null;
  system_prompt: string | null;
  user_prompt: string | null;
  tool_calls_json: string | null;
  judge_is_badcase: number | null;
  judge_reason: string | null;
  judge_dimensions_json: string | null;
  human_notes: string | null;
  human_labeled_at: string | null;
}

export interface GroundTruthRow extends DegradedEnvelope {
  id: number;
  session_id: string | null;
  tick_id: number | null;
  chat_name: string | null;
  judge_score: number | null;
  judge_is_badcase: number | null;
  human_is_badcase: number | null;
  human_badcase_type: string | null;
  raw_response: string | null;
  /** true when the human label contradicts the judge's verdict. */
  disagree: boolean;
}

export interface ReviewRow extends DegradedEnvelope {
  id: number;
  draft_id: string;
  chat_name: string | null;
  status: string | null;
  badcase_type: string | null;
  severity: string | null;
  confidence: number | null;
  overall_score: number | null;
  judge_reason: string | null;
}

export interface ScreenshotRow {
  tick_id: number;
  session_id: string | null;
  chat_name: string | null;
  screenshot_path: string | null;
  created_at: string | null;
  has_image: boolean;
}

export interface ScreenshotListResponse extends DegradedEnvelope {
  total: number;
  page: number;
  size: number;
  rows: ScreenshotRow[];
}

export interface ScreenshotDetail extends DegradedEnvelope {
  tick_id: number;
  session_id: string | null;
  chat_name: string | null;
  screenshot_path: string | null;
  created_at: string | null;
  has_image: boolean;
  reply: string | null;
  judge_score: number | null;
  skip_reason: string | null;
}

export interface BenchmarkReport {
  key: "judge" | "reply";
  title: string;
  /** Whole report as a self-contained HTML document, injected via v-html. */
  html: string;
  modified: number | null;
}

export interface BenchmarkBundle extends DegradedEnvelope {
  reports: BenchmarkReport[];
  refreshable: boolean;
}

/** `SELECT *` from the experiments table. */
export interface Experiment {
  id: number;
  name: string | null;
  description: string | null;
  status: string | null;
  control_arm: string | null;
  experiment_arm: string | null;
  created_at: string | null;
  [extra: string]: unknown;
}

export interface ExperimentResult {
  tick_id: number;
  chat_name: string | null;
  created_at: string | null;
  c_reply: string | null;
  e_reply: string | null;
  c_reason: string | null;
  e_reason: string | null;
  c_score: number | null;
  e_score: number | null;
  c_bc: number | null;
  e_bc: number | null;
  c_dims: string | null;
  e_dims: string | null;
  /** The prompts the arms actually saw, keyed by tick id in the envelope. */
  context?: ExperimentContext;
}

export interface ExperimentContext {
  system_prompt: string | null;
  user_prompt: string | null;
  tool_calls_json: string | null;
}

export interface ExperimentDetail extends DegradedEnvelope {
  experiment: Experiment;
  results: ExperimentResult[];
  totals: {
    count: number;
    c_avg: number | null;
    e_avg: number | null;
    c_badcase: number;
    e_badcase: number;
  };
}

export type CodeAuditStatus =
  | "pending"
  | "todo"
  | "rethink"
  | "fixed"
  | "wontfix"
  | "deferred"
  | "ai_analyzing"
  | "failed";

export const CODE_AUDIT_STATUSES: readonly CodeAuditStatus[] = [
  "pending",
  "todo",
  "rethink",
  "fixed",
  "wontfix",
  "deferred",
  "ai_analyzing",
  "failed",
] as const;

export const CODE_AUDIT_STATUS_LABELS: Record<CodeAuditStatus, string> = {
  pending: "待处理",
  todo: "已认领",
  rethink: "待重做",
  fixed: "已修复",
  wontfix: "不修",
  deferred: "搁置",
  ai_analyzing: "分析中",
  failed: "分析失败",
};

export interface CodeAuditIssue {
  key: string;
  severity: string;
  title: string;
  category: string | null;
  file: string | null;
  line: number | null;
  description: string | null;
}

export interface CodeAuditState {
  status: CodeAuditStatus;
  notes: string;
  ai_proposal: string;
}

export interface CodeAuditRound {
  round: number;
  notes: string;
  proposal: string;
  created_at: string | null;
}

export interface CodeAuditListResponse extends DegradedEnvelope {
  issues: CodeAuditIssue[];
  states: Record<string, CodeAuditState>;
}

export interface CodeAuditDetailResponse extends CodeAuditState, DegradedEnvelope {
  issue: CodeAuditIssue;
  rounds: CodeAuditRound[];
}

export type WikiAction = "delete" | "fix" | "mark" | "skip";

export const WIKI_ACTIONS: readonly WikiAction[] = ["delete", "fix", "mark", "skip"] as const;

export const WIKI_ACTION_LABELS: Record<WikiAction, string> = {
  delete: "删除该行",
  fix: "改为新值",
  mark: "标记确认",
  skip: "跳过",
};

export interface WikiReviewItem {
  id: string;
  wiki: string;
  is_group: boolean;
  fact: string | null;
  line: string | null;
  wiki_excerpt: string | null;
  [extra: string]: unknown;
}

export interface WikiDecision {
  id: string;
  wiki: string;
  is_group: boolean;
  line: string;
  action: WikiAction;
  new_value: string;
  decided_at: string;
}

export interface WikiReviewListResponse extends DegradedEnvelope {
  items: WikiReviewItem[];
  decisions: Record<string, WikiDecision>;
  pending: number;
}

export interface WikiReviewDetailResponse extends DegradedEnvelope {
  item: WikiReviewItem;
  decision: WikiDecision | null;
  /** The offending line plus ±2 lines, line-numbered, for locating it. */
  context: string;
}
