<script setup lang="ts">
// A/B experiment list. A row is `SELECT *` off the experiments table, so the
// model is deliberately open-ended: the columns we understand get real columns
// and everything else stays reachable behind a per-row popover, rather than
// being either dropped or dumped across the screen.

import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { MoreFilled, Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { Experiment } from "@shared/types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";

/** Rendered as real table columns; every other key falls to the popover. */
const MODELLED_COLUMNS = new Set<string>([
  "id",
  "name",
  "description",
  "status",
  "control_arm",
  "experiment_arm",
  "created_at",
]);

/**
 * `status` is free text, not an enum. The `experiments` table in
 * src/badcase/case_db.py has no status column at all today, so this is a
 * display vocabulary for whatever a future writer puts there rather than a
 * closed set: an unknown value falls through to its raw text on a neutral tag
 * instead of leaving the cell blank.
 */
const STATUS_LABELS: Record<string, { label: string; type: TagType }> = {
  pending: { label: "待运行", type: "info" },
  running: { label: "运行中", type: "warning" },
  ok: { label: "完成", type: "success" },
  done: { label: "完成", type: "success" },
  failed: { label: "失败", type: "danger" },
  error: { label: "失败", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

interface ExtraField {
  key: string;
  value: string;
}

const router = useRouter();

const experiments = ref<Experiment[]>([]);
const loading = ref(false);
const loadError = ref("");

const extrasByRow = computed<Map<number, ExtraField[]>>(() => {
  const map = new Map<number, ExtraField[]>();
  for (const row of experiments.value) {
    const fields: ExtraField[] = [];
    for (const [key, value] of Object.entries(row)) {
      if (MODELLED_COLUMNS.has(key) || value === undefined) continue;
      // Object and array values are skipped: rendering them as a flat cell
      // would stringify to "[object Object]" / "a,b,c", which reads as data
      // and is not. Every scalar the row carries stays reachable.
      if (value !== null && typeof value === "object") continue;
      fields.push({ key, value: typeof value === "string" ? value : String(value) });
    }
    map.set(row.id, fields);
  }
  return map;
});

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function text(value: string | null): string {
  return typeof value === "string" && value.trim() ? value : DASH;
}

function truncate(value: string | null, max = 60): string {
  const raw = text(value);
  return raw.length > max ? `${raw.slice(0, max)}…` : raw;
}

function statusView(value: string | null): { label: string; type: TagType } | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const key = value.trim().toLowerCase();
  return STATUS_LABELS[key] ?? { label: value.trim(), type: "info" };
}

/** Arms are free text too; a nameless arm still needs a readable header. */
function arm(value: string | null, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

/** SQLite writes `YYYY-MM-DD HH:MM:SS`; an ISO sender would use a `T`. */
function stamp(value: string | null): string {
  if (typeof value !== "string" || !value.trim()) return DASH;
  return value.trim().replace("T", " ").slice(0, 19);
}

function extrasFor(id: number): ExtraField[] {
  return extrasByRow.value.get(id) ?? [];
}

function detailLink(id: number): { name: string; params: { id: string } } {
  return { name: "experiment-detail", params: { id: String(id) } };
}

function open(id: number): void {
  void router.push(detailLink(id));
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    // The server already orders by id desc, so newest-first needs no client sort.
    experiments.value = (await api.experiments()).experiments;
    loadError.value = "";
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">EXPERIMENTS · 实验 A/B</p>
        <h1>实验 A/B</h1>
        <p class="head-note muted">同一 tick 由对照组与实验组各回答一次，共 {{ experiments.length }} 个实验</p>
      </div>
      <div class="head-actions">
        <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="loadError" />

    <section v-if="!loading && !loadError && !experiments.length" class="card">
      <el-empty description="没有实验记录">
        <p class="empty-body muted">
          实验由批处理脚本写入：在仓库根目录执行
          <code class="mono">python3 tools/bench/run_experiment.py --exp &lt;名称&gt; --all-labeled</code>
          后回到本页刷新。
        </p>
        <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </el-empty>
    </section>

    <section v-else class="card table-card">
      <el-table
        v-loading="loading"
        :data="experiments"
        row-key="id"
        size="small"
        class="experiments"
        :empty-text="loadError ? '加载失败，请查看上方提示后刷新' : '没有实验记录'"
        @row-click="(row: Experiment) => open(row.id)"
      >
        <el-table-column label="#" width="62">
          <template #default="scope">
            <span class="mono">{{ scope.row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column label="名称" min-width="180">
          <template #default="scope">
            <router-link class="name-link" :to="detailLink(scope.row.id)" @click.stop>
              {{ text(scope.row.name) }}
            </router-link>
          </template>
        </el-table-column>

        <el-table-column label="描述" min-width="220" show-overflow-tooltip>
          <template #default="scope">
            <span class="desc">{{ truncate(scope.row.description) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="94">
          <template #default="scope">
            <el-tag
              v-if="statusView(scope.row.status)"
              size="small"
              :type="statusView(scope.row.status)?.type"
              effect="plain"
              disable-transitions
            >
              {{ statusView(scope.row.status)?.label }}
            </el-tag>
            <span v-else class="muted">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="对照组" min-width="140">
          <template #default="scope">
            <span class="arm control">{{ arm(scope.row.control_arm, "对照组") }}</span>
          </template>
        </el-table-column>

        <el-table-column label="实验组" min-width="140">
          <template #default="scope">
            <span class="arm experiment">{{ arm(scope.row.experiment_arm, "实验组") }}</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="scope">
            <span class="mono when">{{ stamp(scope.row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="更多" width="72" align="right">
          <template #default="scope">
            <el-popover
              v-if="extrasFor(scope.row.id).length"
              placement="left"
              :width="320"
              trigger="click"
            >
              <template #reference>
                <el-button link size="small" :icon="MoreFilled" @click.stop>更多</el-button>
              </template>
              <dl class="extras">
                <div v-for="field in extrasFor(scope.row.id)" :key="field.key" class="extra-row">
                  <dt class="mono">{{ field.key }}</dt>
                  <dd class="mono">{{ field.value === "" ? "(空字符串)" : field.value }}</dd>
                </div>
              </dl>
              <p class="muted extras-note">对象与数组类型的列不在此处展示。</p>
            </el-popover>
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

.table-card {
  padding: 0;
  overflow: hidden;
}

.experiments :deep(.el-table__cell) {
  padding: 8px 0;
}

.empty-body {
  margin: 0 auto 14px;
  max-width: 520px;
  font-size: 12px;
  line-height: 1.7;
}

.name-link {
  color: var(--green-dark);
  font-size: 12.5px;
  text-decoration: none;
}

.name-link:hover {
  text-decoration: underline;
}

.desc {
  font-size: 11.5px;
  color: var(--muted);
}

.arm {
  font-size: 11.5px;
  border-radius: var(--radius);
  padding: 1px 6px;
  border: 1px solid transparent;
}

.arm.control {
  color: var(--path-vision);
  background: var(--path-vision-soft);
  border-color: var(--path-vision);
}

.arm.experiment {
  color: var(--path-element);
  background: var(--path-element-soft);
  border-color: var(--path-element);
}

.when {
  color: var(--muted);
  font-size: 11px;
}

.extras {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 320px;
  overflow: auto;
}

.extra-row {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: 10px;
  font-size: 11px;
}

.extra-row dt {
  color: var(--muted);
  word-break: break-all;
}

.extra-row dd {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.extras-note {
  margin: 8px 0 0;
  font-size: 10px;
}
</style>
