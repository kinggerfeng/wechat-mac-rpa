<script setup lang="ts">
// Live execution trace, docked under the canvas.
//
// Each row is one node attempt. The two arrival phases are merged upstream, so a
// node appears greyed while it runs and resolves to its real status when it
// settles — which is what makes a run stalled on a permission prompt readable
// instead of a blank panel.

import { computed } from "vue";
import { CircleCheck, CircleClose, Loading, Minus, VideoPause } from "@element-plus/icons-vue";
import { useTraceStore } from "../../stores/trace";

const props = defineProps<{ runningNodeIds: string[] }>();
const emit = defineEmits<{ (e: "abort"): void }>();

const trace = useTraceStore();

const rows = computed(() =>
  [...trace.nodes].sort((a, b) => a.startedAt - b.startedAt),
);

function iconFor(status: string) {
  if (status === "ok") return CircleCheck;
  if (status === "error") return CircleClose;
  if (status === "running") return Loading;
  return Minus;
}

/** The dual-path provenance, when the node recorded one. */
function sourceOf(outputs: Record<string, unknown>): "element" | "vision" | null {
  const value = outputs?.source;
  return value === "element" || value === "vision" ? value : null;
}

function durationOf(node: { durationMs: number | null; pending: boolean }): string {
  if (node.pending) return "执行中";
  if (node.durationMs === null) return "—";
  return node.durationMs < 1000 ? `${node.durationMs}ms` : `${(node.durationMs / 1000).toFixed(1)}s`;
}
</script>

<template>
  <section class="trace">
    <header class="trace-head">
      <p class="eyebrow">EXECUTION TRACE</p>
      <div class="trace-meta">
        <template v-if="trace.run">
          <span class="mono run-id">{{ trace.run.id }}</span>
          <el-tag
            size="small"
            :type="trace.live ? 'warning' : trace.run.status === 'ok' ? 'success' : 'danger'"
            effect="plain"
          >
            {{ trace.live ? "运行中" : trace.run.status }}
          </el-tag>
          <span class="muted stats">{{ trace.run.steps }} 步 · {{ (trace.totalMs / 1000).toFixed(1) }}s</span>
        </template>
        <span v-else class="muted stats">尚未运行</span>
        <el-button
          v-if="trace.live"
          size="small"
          type="danger"
          plain
          :icon="VideoPause"
          @click="emit('abort')"
        >
          中止
        </el-button>
      </div>
    </header>

    <div class="trace-body">
      <p v-if="!rows.length" class="empty">
        运行流程后，这里会按节点逐步显示实际发生了什么。
      </p>

      <ol v-else class="rows">
        <li
          v-for="row in rows"
          :key="row.key"
          class="row"
          :class="[row.status, { pending: row.pending, active: props.runningNodeIds.includes(row.nodeId) }]"
        >
          <span class="row-icon">
            <el-icon><component :is="iconFor(row.pending ? 'running' : row.status)" /></el-icon>
          </span>
          <div class="row-main">
            <div class="row-title">
              <span class="row-name">{{ row.name }}</span>
              <span class="row-type mono">{{ row.nodeType }}</span>
              <span v-if="row.attempt > 1" class="attempt">第 {{ row.attempt }} 次</span>
              <span
                v-if="sourceOf(row.outputs)"
                class="source-tag"
                :class="sourceOf(row.outputs)"
              >
                {{ sourceOf(row.outputs) === "element" ? "元素库" : "多模态" }}
              </span>
            </div>
            <p v-if="row.error" class="row-error">{{ row.error }}</p>
            <p v-else-if="row.outputs?.__branch__" class="row-branch">
              分支 → {{ row.outputs.__branch__ }}
            </p>
            <p
              v-else-if="row.outputs?.message"
              class="row-note"
            >{{ row.outputs.message }}</p>
          </div>
          <span class="row-duration mono">{{ durationOf(row) }}</span>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.trace {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--line);
  background: #fbfcfa;
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 16px;
  border-bottom: 1px solid var(--line);
}

.eyebrow {
  margin: 0;
  color: #809087;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1.3px;
}

.trace-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.run-id {
  color: #8a9790;
  font-size: 10px;
}

.stats {
  font-size: 10.5px;
}

.trace-body {
  max-height: 250px;
  overflow-y: auto;
}

.empty {
  margin: 0;
  padding: 26px 16px;
  color: #9aa59e;
  font-size: 11.5px;
  text-align: center;
}

.rows {
  margin: 0;
  padding: 0;
  list-style: none;
}

.row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 6px 16px;
  border-bottom: 1px solid #eef2ef;
}

.row.active {
  background: #f4faf6;
}

.row-icon {
  margin-top: 2px;
  font-size: 13px;
}

.row.ok .row-icon { color: #52a27c; }
.row.error .row-icon { color: #d0642f; }
.row.pending .row-icon { color: #b07d2b; }
.row.skipped .row-icon { color: #a9b5ae; }

.row-main {
  min-width: 0;
  flex: 1;
}

.row-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.row-name {
  color: #2c3a34;
  font-size: 11.5px;
  font-weight: 600;
}

.row-type {
  color: #93a098;
  font-size: 9.5px;
}

.attempt {
  padding: 0 5px;
  border-radius: 2px;
  background: #f7efdd;
  color: #8a6420;
  font-size: 9px;
}

.source-tag {
  padding: 1px 6px;
  border-radius: 2px;
  font-size: 9px;
  font-weight: 600;
}

.source-tag.element {
  background: var(--path-element-soft);
  color: var(--path-element);
}

.source-tag.vision {
  background: var(--path-vision-soft);
  color: var(--path-vision);
}

.row-error {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  color: #a4552f;
  font-size: 10.5px;
  line-height: 1.45;
}

.row-branch,
.row-note {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  color: #6a7a72;
  font-size: 10.5px;
  line-height: 1.45;
}

.row-duration {
  flex: 0 0 auto;
  color: #8a9790;
  font-size: 10px;
}
</style>
