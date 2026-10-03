<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { Refresh, VideoPause } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import { useEngineStore } from "../stores/engine";
import { useTraceStore, type TraceNode } from "../stores/trace";
import type { Run, RunStatus, SpanStatus } from "@shared/types";

const DASH = "—";
const REFRESH_MS = 2000;

const RUN_STATUS: Record<RunStatus, { label: string; type: "success" | "info" | "warning" | "danger" }> = {
  ok: { label: "成功", type: "success" },
  running: { label: "运行中", type: "warning" },
  error: { label: "失败", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

const SPAN_LABEL: Record<SpanStatus, string> = {
  ok: "成功",
  running: "执行中",
  error: "失败",
  skipped: "跳过",
};

const engine = useEngineStore();
const trace = useTraceStore();

const runs = ref<Run[]>([]);
const loading = ref(false);
const loadError = ref("");
const expandedId = ref<string | null>(null);
const traceBusy = ref(false);
const aborting = ref<string | null>(null);
const abortError = ref("");
const openScopes = ref<Record<string, string[]>>({});

let refreshTimer: number | null = null;

const runningCount = computed(() => runs.value.filter((run) => run.status === "running").length);
const traceNodes = computed<TraceNode[]>(() => trace.nodes);
/** Run-time scope — the executor's scratch variables, not the flow's inputs. */
const scopeEntries = computed<[string, unknown][]>(() =>
  trace.run?.scope ? Object.entries(trace.run.scope) : [],
);
const scopeModel = computed<string[]>(() => (expandedId.value ? openScopes.value[expandedId.value] ?? [] : []));

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function timeAgo(ts: number | null): string {
  if (!ts) return DASH;
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

function flowLabel(run: Run): string {
  return run.flow_name || run.flow_id;
}

/** The el-table slot hands out `any` rows; this keeps the lookup typed. */
function runStatus(run: Run): { label: string; type: "success" | "info" | "warning" | "danger" } {
  return RUN_STATUS[run.status];
}

function duration(run: Run): string {
  if (!run.ended_at) return DASH;
  return `${Math.max(0, Math.round((run.ended_at - run.started_at) * 1000))} ms`;
}

function triggerLabel(run: Run): string {
  return run.trigger_ref ? `${run.trigger_type} · ${run.trigger_ref}` : run.trigger_type;
}

function truncate(text: string | null, max = 52): string {
  if (!text) return DASH;
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function nodeDuration(node: TraceNode): string {
  return node.durationMs === null ? DASH : `${node.durationMs} ms`;
}

/** Which branch of the dual-path design actually resolved this node. */
function pathSource(node: TraceNode): "element" | "vision" | null {
  const value = node.outputs.source;
  return value === "element" || value === "vision" ? value : null;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return DASH;
  if (typeof value === "string") return value;
  try {
    return truncate(JSON.stringify(value) ?? DASH, 400);
  } catch {
    return String(value);
  }
}

function isActive(row: Run | null): boolean {
  return !!row && row.id === expandedId.value;
}

function onScopeChange(value: (string | number)[] | string | number): void {
  const id = expandedId.value;
  if (!id) return;
  const next = (Array.isArray(value) ? value : [value]).map((item) => String(item));
  openScopes.value = { ...openScopes.value, [id]: next };
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    runs.value = (await api.runs()).runs;
    loadError.value = "";
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

async function loadTrace(runId: string): Promise<void> {
  traceBusy.value = true;
  try {
    await trace.attach(runId);
  } finally {
    traceBusy.value = false;
  }
}

function onExpandChange(row: Run, expanded: Run[]): void {
  const isOpen = expanded.some((item) => item.id === row.id);
  if (!isOpen) {
    if (expandedId.value === row.id) expandedId.value = null;
    return;
  }
  expandedId.value = row.id;
  void loadTrace(row.id);
}

async function abort(run: Run): Promise<void> {
  aborting.value = run.id;
  abortError.value = "";
  try {
    await api.abortRun(run.id);
    await load();
  } catch (error) {
    abortError.value = reason(error);
  } finally {
    aborting.value = null;
  }
}

// Only poll while something is actually executing — a finished history is
// static, and a 2s timer over it would just burn the loopback API.
watch(
  runningCount,
  (count) => {
    if (count > 0 && refreshTimer === null) {
      refreshTimer = window.setInterval(() => void load(), REFRESH_MS);
    } else if (count === 0 && refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
  },
  { immediate: true },
);

onMounted(() => void load());

onUnmounted(() => {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  trace.detach();
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">RUNS · 运行记录</p>
        <h1>运行记录</h1>
        <p class="head-note muted">
          {{ runs.length }} 条 · 数据更新于 {{ engine.lastUpdated || DASH }}
        </p>
      </div>
      <div class="head-actions">
        <span v-if="runningCount" class="live"><span class="dot" />{{ runningCount }} 条执行中 · 每 2 秒刷新</span>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：无法读取运行记录。"
    />
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="loadError"
    />
    <el-alert
      v-if="abortError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="abortError"
    />

    <section class="card table-card">
      <el-table
        v-loading="loading"
        :data="runs"
        row-key="id"
        size="small"
        class="runs"
        empty-text="暂无运行记录"
        @expand-change="onExpandChange"
      >
        <el-table-column type="expand" width="34">
          <template #default="scope">
            <div v-if="isActive(scope.row)" class="detail">
              <div class="detail-head">
                <span class="detail-id mono">{{ expandedId }}</span>
                <el-tag v-if="trace.live" size="small" type="warning" disable-transitions>实时追踪</el-tag>
                <span v-else-if="trace.connected" class="muted detail-note">追踪已连接</span>
              </div>

              <el-skeleton v-if="traceBusy" :rows="2" animated />
              <p v-else-if="!traceNodes.length" class="muted detail-empty">该运行没有执行轨迹。</p>
              <ol v-else class="timeline">
                <li
                  v-for="node in traceNodes"
                  :key="node.key"
                  class="node"
                  :class="`is-${node.status}`"
                >
                  <span class="node-dot" />
                  <div class="node-body">
                    <div class="node-line">
                      <span class="node-name">{{ node.name }}</span>
                      <span class="node-type mono">{{ node.nodeType }}</span>
                      <span v-if="pathSource(node) === 'element'" class="path-tag element">元素库</span>
                      <span v-else-if="pathSource(node) === 'vision'" class="path-tag vision">多模态</span>
                      <span v-if="node.attempt > 1" class="node-retry mono">第 {{ node.attempt }} 次</span>
                      <span class="node-status">{{ SPAN_LABEL[node.status] }}</span>
                      <span class="node-time mono">{{ nodeDuration(node) }}</span>
                    </div>
                    <p v-if="node.error" class="node-error mono">{{ node.error }}</p>
                  </div>
                </li>
              </ol>

              <el-collapse
                v-if="scopeEntries.length"
                class="scope"
                :model-value="scopeModel"
                @change="onScopeChange"
              >
                <el-collapse-item :title="`运行期变量 (${scopeEntries.length})`" name="scope">
                  <dl class="vars">
                    <div v-for="[key, value] in scopeEntries" :key="key" class="var-row">
                      <dt class="mono">{{ key }}</dt>
                      <dd class="mono">{{ formatValue(value) }}</dd>
                    </div>
                  </dl>
                </el-collapse-item>
              </el-collapse>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="流程" min-width="190">
          <template #default="scope">
            <div class="cell-flow">
              <span class="flow-name">{{ flowLabel(scope.row) }}</span>
              <span class="flow-id mono">{{ scope.row.id }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="92">
          <template #default="scope">
            <el-tag size="small" :type="runStatus(scope.row).type" disable-transitions>
              {{ runStatus(scope.row).label }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="步数" width="70" align="right">
          <template #default="scope">
            <span class="mono">{{ scope.row.steps }}</span>
          </template>
        </el-table-column>

        <el-table-column label="触发方式" width="160">
          <template #default="scope">
            <span class="mono trigger">{{ triggerLabel(scope.row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="开始时间" width="112">
          <template #default="scope">
            <span class="mono when">{{ timeAgo(scope.row.started_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="耗时" width="94" align="right">
          <template #default="scope">
            <span class="mono">{{ duration(scope.row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="错误" min-width="200">
          <template #default="scope">
            <span class="err" :title="scope.row.error || ''">{{ truncate(scope.row.error) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="88" align="right">
          <template #default="scope">
            <el-button
              v-if="scope.row.status === 'running'"
              size="small"
              type="danger"
              plain
              :icon="VideoPause"
              :loading="aborting === scope.row.id"
              @click.stop="abort(scope.row)"
            >
              中止
            </el-button>
            <span v-else class="muted">{{ DASH }}</span>
          </template>
        </el-table-column>
      </el-table>
    </section>
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

.page-head h1 {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 400;
  letter-spacing: 0.5px;
}

.head-note {
  margin: 8px 0 0;
  font-size: 11px;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.live {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--path-auto);
  font-size: 10px;
}

.live .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--path-auto);
}

.notice {
  width: auto;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.runs :deep(.el-table__cell) {
  padding: 7px 0;
}

.runs :deep(.el-table__expanded-cell) {
  padding: 0;
  background: var(--canvas);
}

.cell-flow {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.flow-name {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.flow-id {
  color: var(--muted);
  font-size: 10px;
}

.trigger,
.when {
  color: var(--muted);
  font-size: 11px;
}

.err {
  color: var(--orange);
  font-size: 11px;
  font-family: var(--font-mono);
}

.detail {
  padding: 14px 18px 16px;
  border-bottom: 1px solid var(--line);
}

.detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.detail-id {
  font-size: 11px;
  color: var(--muted);
}

.detail-note {
  font-size: 10px;
}

.detail-empty {
  margin: 0;
  font-size: 12px;
}

.timeline {
  margin: 0;
  padding: 0 0 0 6px;
  list-style: none;
}

.node {
  position: relative;
  padding: 0 0 14px 18px;
  border-left: 1px solid var(--line);
}

.node:last-child {
  border-left-color: transparent;
  padding-bottom: 2px;
}

.node-dot {
  position: absolute;
  left: -4px;
  top: 5px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted);
  box-shadow: 0 0 0 3px var(--canvas);
}

.node.is-ok .node-dot {
  background: var(--green);
}

.node.is-running .node-dot {
  background: var(--path-auto);
}

.node.is-error .node-dot {
  background: var(--orange);
}

.node.is-skipped {
  opacity: 0.55;
}

.node-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.node-name {
  font-size: 12.5px;
}

.node-type {
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 1px 5px;
}

.node-retry {
  font-size: 10px;
  color: var(--orange);
}

.node-status {
  font-size: 10px;
  color: var(--muted);
}

.node-time {
  margin-left: auto;
  font-size: 10px;
  color: var(--muted);
}

.path-tag {
  font-size: 10px;
  line-height: 1.4;
  border-radius: var(--radius);
  padding: 1px 6px;
  border: 1px solid transparent;
}

.path-tag.element {
  color: var(--path-element);
  background: var(--path-element-soft);
  border-color: var(--path-element);
}

.path-tag.vision {
  color: var(--path-vision);
  background: var(--path-vision-soft);
  border-color: var(--path-vision);
}

.node-error {
  margin: 6px 0 0;
  padding: 6px 8px;
  border-left: 2px solid var(--orange);
  background: var(--paper);
  color: var(--orange);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
}

.scope {
  margin-top: 4px;
  border-top: 1px solid var(--line);
}

.vars {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.var-row {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 12px;
  font-size: 11px;
}

.var-row dt {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
}

.var-row dd {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
