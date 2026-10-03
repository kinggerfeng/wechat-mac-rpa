<script setup lang="ts">
// The A/B view: one tick answered twice, side by side. Every score here comes
// back nullable from SQLite and the entire point of the page is comparing
// them — so a missing value renders as a dash, never as a 0 that would read as
// a real score and a delta of NaN.

import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Back, Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { ExperimentContext, ExperimentDetail, ExperimentResult } from "@shared/types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";
type DeltaDir = "up" | "down" | "flat" | "none";

interface DimRow {
  name: string;
  control: number | null;
  experiment: number | null;
  delta: number | null;
}

interface DimView {
  broken: boolean;
  raw: { control: string | null; experiment: string | null };
  rows: DimRow[];
}

interface ContextField {
  label: string;
  value: string;
}

interface TickLink {
  name: string;
  params: { id: string };
}

interface TickView {
  result: ExperimentResult;
  link: TickLink | null;
  delta: number | null;
  dims: DimView;
  context: ContextField[];
}

const route = useRoute();
const router = useRouter();

const experimentId = ref<number | null>(null);
const detail = ref<ExperimentDetail | null>(null);
const loading = ref(false);
const loadError = ref("");
const notFound = ref(false);

const totals = computed(() => detail.value?.totals ?? null);
const experiment = computed(() => detail.value?.experiment ?? null);
const controlArm = computed(() => arm(experiment.value?.control_arm ?? null, "对照组"));
const experimentArm = computed(() => arm(experiment.value?.experiment_arm ?? null, "实验组"));

/** `c_avg` / `e_avg` are null when nothing was scored; the delta follows them. */
const avgDelta = computed<number | null>(() => {
  const t = totals.value;
  if (!t) return null;
  const control = finite(t.c_avg);
  const exp = finite(t.e_avg);
  return control === null || exp === null ? null : exp - control;
});

const ticks = computed<TickView[]>(() =>
  (detail.value?.results ?? []).map((result) => {
    const control = finite(result.c_score);
    const exp = finite(result.e_score);
    return {
      result,
      link: tickLink(result.tick_id),
      delta: control === null || exp === null ? null : exp - control,
      dims: dimView(result.c_dims, result.e_dims),
      context: contextFields(result.context),
    };
  }),
);

/** Status vocabulary shared with ExperimentListPage. Duplicated rather than
 *  imported: the label map lives next to the UI that owns it, and
 *  types/index.ts belongs to another writer. */
const STATUS_LABELS: Record<string, { label: string; type: TagType }> = {
  pending: { label: "待运行", type: "info" },
  running: { label: "运行中", type: "warning" },
  ok: { label: "完成", type: "success" },
  done: { label: "完成", type: "success" },
  failed: { label: "失败", type: "danger" },
  error: { label: "失败", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function statusView(value: string | null): { label: string; type: TagType } | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const key = value.trim().toLowerCase();
  return STATUS_LABELS[key] ?? { label: value.trim(), type: "info" };
}

/** Route params are strings; anything that is not a positive integer is a bad
 *  link and must not reach the API as `NaN`. */
function parseId(raw: unknown): number | null {
  const value = Array.isArray(raw) ? raw[0] : raw;
  if (typeof value !== "string" || !/^[0-9]+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

/** A `number | null` that is really a number. Guards NaN and Infinity the same
 *  way, since neither is a score any reader could act on. */
function finite(value: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: string | null): string {
  return typeof value === "string" && value.trim() ? value : DASH;
}

function arm(value: string | null, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function stamp(value: string | null): string {
  if (typeof value !== "string" || !value.trim()) return DASH;
  return value.trim().replace("T", " ").slice(0, 19);
}

function score(value: number | null): string {
  const n = finite(value);
  if (n === null) return DASH;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function signed(value: number | null): string {
  const n = finite(value);
  if (n === null) return DASH;
  const body = Math.abs(n).toFixed(1);
  if (n > 0) return `+${body}`;
  if (n < 0) return `−${body}`;
  return `±${body}`;
}

function dir(value: number | null): DeltaDir {
  const n = finite(value);
  if (n === null) return "none";
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

/**
 * `judge_is_badcase` is an INTEGER flag where only 1 means a badcase. A truthy
 * check would promote any other non-zero to a badcase and a falsy check would
 * report a missing flag as 正常, so both sides are matched explicitly.
 */
function badcase(value: number | null): { label: string; type: TagType } {
  if (value === 1) return { label: "坏例", type: "danger" };
  if (value === 0) return { label: "正常", type: "success" };
  return { label: "未知", type: "info" };
}

function tickLink(tickId: number): TickLink | null {
  if (!Number.isSafeInteger(tickId) || tickId <= 0) return null;
  return { name: "tick-detail", params: { id: String(tickId) } };
}

function dimScore(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const inner = (value as { score?: unknown }).score;
    if (typeof inner === "number" && Number.isFinite(inner)) return inner;
  }
  return null;
}

type DimSide =
  | { kind: "missing" }
  | { kind: "broken" }
  | { kind: "ok"; value: Record<string, unknown> };

function parseSide(raw: string | null): DimSide {
  if (typeof raw !== "string" || !raw.trim()) return { kind: "missing" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { kind: "broken" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { kind: "broken" };
  return { kind: "ok", value: parsed as Record<string, unknown> };
}

/** One broken arm never blanks the panel: the raw string stays reachable in a
 *  tooltip and whichever arm did parse still contributes its rows. */
function dimView(controlRaw: string | null, experimentRaw: string | null): DimView {
  const control = parseSide(controlRaw);
  const exp = parseSide(experimentRaw);
  const raw = { control: controlRaw, experiment: experimentRaw };
  if (control.kind === "missing" && exp.kind === "missing") {
    return { broken: false, raw, rows: [] };
  }
  const left = control.kind === "ok" ? control.value : {};
  const right = exp.kind === "ok" ? exp.value : {};
  // Control order first, then any dimension only the experiment arm carries.
  const names = [...Object.keys(left), ...Object.keys(right).filter((name) => !(name in left))];
  return {
    broken: control.kind === "broken" || exp.kind === "broken",
    raw,
    rows: names.map((name) => {
      const c = dimScore(left[name]);
      const e = dimScore(right[name]);
      return { name, control: c, experiment: e, delta: c === null || e === null ? null : e - c };
    }),
  };
}

function rawDimText(view: DimView): string {
  const parts: string[] = [];
  if (view.raw.control) parts.push(`对照组: ${view.raw.control}`);
  if (view.raw.experiment) parts.push(`实验组: ${view.raw.experiment}`);
  return parts.join("\n\n");
}

function contextFields(context: ExperimentContext | undefined): ContextField[] {
  if (!context) return [];
  return [
    { label: "System Prompt", value: context.system_prompt },
    { label: "User Prompt", value: context.user_prompt },
    { label: "Tool Calls", value: context.tool_calls_json },
  ].filter((field): field is ContextField => typeof field.value === "string" && field.value.length > 0);
}

async function load(): Promise<void> {
  const id = parseId(route.params.id);
  experimentId.value = id;
  detail.value = null;
  loadError.value = "";
  notFound.value = false;
  // A bad link is rejected before any request: /experiments/abc is a typo, not
  // a backend problem, and a 422 would read like one.
  if (id === null) return;

  loading.value = true;
  try {
    detail.value = await api.experiment(id);
  } catch (error) {
    // A deleted experiment and an unreachable backend are different outcomes;
    // collapsing them would send the operator to debug the wrong thing.
    notFound.value = error instanceof ApiError && error.status === 404;
    loadError.value = notFound.value ? "" : reason(error);
  } finally {
    loading.value = false;
  }
}

watch(() => route.params.id, () => void load(), { immediate: true });
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div class="head-left">
        <el-button size="small" :icon="Back" text @click="router.push({ name: 'experiments' })">
          实验列表
        </el-button>
        <h1>{{ experiment ? text(experiment.name) : "实验详情" }}</h1>
        <el-tag
          v-if="statusView(experiment?.status ?? null)"
          size="small"
          effect="plain"
          disable-transitions
        >
          {{ statusView(experiment?.status ?? null)?.label }}
        </el-tag>
      </div>
      <div class="head-actions">
        <span v-if="experiment" class="mono muted head-arms">
          {{ controlArm }} → {{ experimentArm }} · {{ stamp(experiment.created_at) }}
        </span>
        <el-button
          size="small"
          :icon="Refresh"
          :loading="loading"
          :disabled="experimentId === null"
          @click="load"
        >
          刷新
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="experimentId === null"
      type="error"
      show-icon
      :closable="false"
      title="链接无效"
      description="地址栏中的实验编号不是正整数，未发出任何请求。请从实验列表重新进入。"
    />
    <el-alert
      v-else-if="notFound"
      type="warning"
      show-icon
      :closable="false"
      :title="`实验 #${experimentId} 不存在`"
      description="该实验已被删除或从未写入 cases.db。这不是加载失败。"
    />
    <el-alert v-else-if="loadError" type="error" show-icon :closable="false" :title="loadError" />

    <el-skeleton v-if="loading" class="card skeleton" :rows="6" animated />

    <template v-else-if="detail">
      <section class="card totals">
        <div class="stat">
          <span class="lbl">样本数</span>
          <span class="val mono">{{ totals?.count ?? DASH }}</span>
        </div>
        <div class="stat">
          <span class="lbl">{{ controlArm }} 均分</span>
          <span class="val mono">{{ score(totals?.c_avg ?? null) }}</span>
        </div>
        <div class="stat">
          <span class="lbl">{{ experimentArm }} 均分</span>
          <span class="val mono">{{ score(totals?.e_avg ?? null) }}</span>
        </div>
        <div class="stat">
          <span class="lbl">均分差值</span>
          <span class="val mono delta" :class="dir(avgDelta)">{{ signed(avgDelta) }}</span>
        </div>
        <div class="stat">
          <span class="lbl">{{ controlArm }} 坏例</span>
          <span class="val mono">{{ totals?.c_badcase ?? DASH }}</span>
        </div>
        <div class="stat">
          <span class="lbl">{{ experimentArm }} 坏例</span>
          <span class="val mono">{{ totals?.e_badcase ?? DASH }}</span>
        </div>
      </section>

      <section v-if="!ticks.length" class="card">
        <el-empty description="该实验还没有任何 tick 结果">
          <p class="empty-body muted">实验行已写入，但没有任何 tick 被两臂同时评测过。</p>
        </el-empty>
      </section>

      <section v-for="tick in ticks" :key="tick.result.tick_id" class="card tick">
        <div class="tick-head">
          <h2>
            <router-link v-if="tick.link" :to="tick.link">
              #{{ tick.result.tick_id }}
            </router-link>
            <span v-else class="mono">#{{ DASH }}</span>
          </h2>
          <span class="muted tick-meta">
            {{ text(tick.result.chat_name) }} · {{ stamp(tick.result.created_at) }}
          </span>
          <span class="mono delta big" :class="dir(tick.delta)">{{ signed(tick.delta) }}</span>
        </div>

        <div class="arms">
          <article class="arm-block control">
            <header>
              <span class="arm-name">{{ controlArm }}</span>
              <el-tag size="small" :type="badcase(tick.result.c_bc).type" effect="plain" disable-transitions>
                {{ badcase(tick.result.c_bc).label }}
              </el-tag>
              <span class="mono arm-score">{{ score(tick.result.c_score) }} 分</span>
            </header>
            <p class="reply">{{ text(tick.result.c_reply) }}</p>
            <p class="reason muted">理由：{{ text(tick.result.c_reason) }}</p>
          </article>

          <article class="arm-block experiment">
            <header>
              <span class="arm-name">{{ experimentArm }}</span>
              <el-tag size="small" :type="badcase(tick.result.e_bc).type" effect="plain" disable-transitions>
                {{ badcase(tick.result.e_bc).label }}
              </el-tag>
              <span class="mono arm-score">{{ score(tick.result.e_score) }} 分</span>
            </header>
            <p class="reply">{{ text(tick.result.e_reply) }}</p>
            <p class="reason muted">理由：{{ text(tick.result.e_reason) }}</p>
          </article>
        </div>

        <div class="dims">
          <h3 class="dims-title">维度得分</h3>
          <table v-if="tick.dims.rows.length" class="dims-table">
            <thead>
              <tr>
                <th>维度</th>
                <th class="num">{{ controlArm }}</th>
                <th class="num">{{ experimentArm }}</th>
                <th class="num">差值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in tick.dims.rows" :key="row.name">
                <td>{{ row.name }}</td>
                <td class="num mono">{{ score(row.control) }}</td>
                <td class="num mono">{{ score(row.experiment) }}</td>
                <td class="num mono delta" :class="dir(row.delta)">{{ signed(row.delta) }}</td>
              </tr>
            </tbody>
          </table>

          <el-alert
            v-if="tick.dims.broken"
            type="warning"
            show-icon
            :closable="false"
            title="维度数据无法解析"
          >
            <template #default>
              <el-tooltip placement="top" :content="rawDimText(tick.dims)">
                <span class="raw-hint">原始 JSON 字符串见此处提示（悬停查看）</span>
              </el-tooltip>
            </template>
          </el-alert>
          <p v-else-if="!tick.dims.rows.length" class="muted dims-empty">该 tick 无维度数据</p>
        </div>

        <el-collapse v-if="tick.context.length" class="context">
          <el-collapse-item title="Prompt 上下文（两臂共用）">
            <div v-for="field in tick.context" :key="field.label" class="context-field">
              <span class="context-label mono">{{ field.label }}</span>
              <pre class="context-body mono">{{ field.value }}</pre>
            </div>
          </el-collapse-item>
        </el-collapse>
        <p v-else class="muted context-empty">该 tick 无 prompt 记录</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  padding: 24px 28px 40px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.page-head h1 {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 400;
  letter-spacing: 0.5px;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.head-arms {
  font-size: 11px;
}

.skeleton {
  padding: 18px 20px;
}

.empty-body {
  margin: 0 auto;
  max-width: 520px;
  font-size: 12px;
}

.totals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1px;
  padding: 0;
  overflow: hidden;
  background: var(--line);
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  background: var(--paper);
}

.stat .lbl {
  font-size: 10px;
  color: var(--muted);
}

.stat .val {
  font-size: 19px;
}

.tick {
  padding: 14px 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.tick-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}

.tick-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.tick-head h2 a {
  color: var(--green-dark);
  text-decoration: none;
}

.tick-head h2 a:hover {
  text-decoration: underline;
}

.tick-meta {
  font-size: 11px;
}

.delta.big {
  margin-left: auto;
  font-size: 16px;
}

/* Improvement is green, a regression is the same orange the run log uses for
   errors, and a delta that could not be computed stays muted so a dash is
   never mistaken for "no change". */
.delta.up {
  color: var(--green);
}

.delta.down {
  color: var(--orange);
}

.delta.flat,
.delta.none {
  color: var(--muted);
}

.arms {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}

.arm-block {
  border: 1px solid var(--line);
  border-left-width: 3px;
  border-radius: var(--radius);
  padding: 10px 12px;
  background: var(--paper);
}

.arm-block.control {
  border-left-color: var(--path-vision);
}

.arm-block.experiment {
  border-left-color: var(--path-element);
}

.arm-block header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.arm-name {
  font-size: 12px;
  font-weight: 600;
}

.arm-score {
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
}

.reply {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.reason {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.dims-title {
  margin: 0 0 6px;
  font-size: 11px;
  color: var(--muted);
  font-weight: 600;
}

.dims-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}

.dims-table th,
.dims-table td {
  padding: 4px 8px;
  border-bottom: 1px solid var(--line);
  text-align: left;
}

.dims-table th {
  color: var(--muted);
  font-size: 10px;
}

.dims-table .num {
  text-align: right;
}

.dims-empty {
  margin: 0;
  font-size: 11px;
}

.raw-hint {
  cursor: help;
  text-decoration: underline dotted;
}

.context {
  border-top: 1px solid var(--line);
}

.context-field {
  margin-bottom: 10px;
}

.context-label {
  display: block;
  font-size: 10px;
  color: var(--muted);
  margin-bottom: 4px;
}

.context-body {
  margin: 0;
  max-height: 300px;
  overflow: auto;
  padding: 8px 10px;
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.context-empty {
  margin: 0;
  font-size: 11px;
}
</style>
