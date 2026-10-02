<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import type { ReviewRow } from "../types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";

/** The endpoint has no unbounded mode, so 全部 is a high sentinel. */
const ALL_LIMIT = 2000;

const LIMIT_OPTIONS = [
  { label: "50 条", value: 50 },
  { label: "100 条", value: 100 },
  { label: "200 条", value: 200 },
  { label: "全部", value: ALL_LIMIT },
];

/**
 * `status` / `severity` / `badcase_type` are free-text columns, not enums —
 * every map below falls through to the raw value so a value nobody listed yet
 * shows up as itself instead of an empty cell.
 *
 * The vocabularies come from the writers of the `cases` table:
 * status from `CaseDB.migrate_from_json` (committed / pending / dismissed) and
 * `JudgeWorker._auto_commit`; severity from `judge_worker._empty_judge_result`
 * (P0 / P1 / P2); badcase_type from the judge's own output schema in
 * `judge_worker.py` plus `redundant_tool_call`, which the auto-commit
 * allow-list accepts but the prompt never asks for.
 */
const STATUS_LABEL: Record<string, { label: string; type: TagType } | undefined> = {
  pending: { label: "待处理", type: "info" },
  committed: { label: "已入库", type: "success" },
  dismissed: { label: "已忽略", type: "warning" },
};

const SEVERITY_LABEL: Record<string, { label: string; type: TagType } | undefined> = {
  P0: { label: "P0 · 严重", type: "danger" },
  P1: { label: "P1 · 较重", type: "warning" },
  P2: { label: "P2 · 轻微", type: "info" },
};

const BADCASE_LABEL: Record<string, string | undefined> = {
  hallucination: "幻觉",
  time_misread: "时间理解错误",
  over_reply: "过度回复",
  info_incomplete: "信息不完整",
  wrong_topic: "话题跑偏",
  bad_format: "格式错误",
  missing_tool_call: "漏调工具",
  redundant_tool_call: "多余工具调用",
  contradiction: "自相矛盾",
  wrong_fact: "事实错误",
  none: "无问题",
};

const engine = useEngineStore();

const rows = ref<ReviewRow[]>([]);
const loading = ref(false);
const loadError = ref("");
const limit = ref(50);

/** A failure must not read as "the case library is empty". */
const isEmpty = computed(() => !loading.value && !loadError.value && rows.value.length === 0);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function statusTag(raw: string | null): { label: string; type: TagType } {
  if (!raw) return { label: DASH, type: "info" };
  return STATUS_LABEL[raw] ?? { label: raw, type: "info" };
}

function severityTag(raw: string | null): { label: string; type: TagType } {
  if (!raw) return { label: DASH, type: "info" };
  return SEVERITY_LABEL[raw] ?? { label: raw, type: "info" };
}

function badcaseLabel(raw: string | null): string {
  if (!raw) return DASH;
  return BADCASE_LABEL[raw] ?? raw;
}

/**
 * `confidence` is a 0..1 fraction and the legacy page printed it with `:.0%`.
 * A value above 1 is not a fraction, and multiplying it would print a
 * confident, wrong "4200%" — so it is shown as written instead of guessing
 * which scale the row meant. A non-finite value lands on the dash, never 0.
 */
function confidence(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return DASH;
  if (value > 1) return String(value);
  return `${(value * 100).toFixed(0)}%`;
}

/** Same 0-100 judge scale as the ground-truth page, so the same one decimal. */
function overall(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return DASH;
  return value.toFixed(1);
}

function truncate(text: string | null, max = 60): string {
  if (!text) return DASH;
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function onLimitChange(value: unknown): void {
  const next = Number(value);
  if (!Number.isFinite(next) || next === limit.value) return;
  limit.value = next;
  void load();
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    rows.value = (await api.reviews(limit.value)).rows;
  } catch (error) {
    // Keep the previous rows: blanking the table on a transient failure is
    // indistinguishable from "the case library is empty".
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
        <p class="eyebrow">REVIEWS · 案例库</p>
        <h1>案例库</h1>
        <p class="head-note muted">
          Judge 打出来的 badcase 草稿。状态、类型、严重度三列都是数据库里的自由文本，
          映射表只覆盖常见值，其余原样显示。数据更新于 {{ engine.lastUpdated || DASH }}。
        </p>
      </div>
      <div class="head-actions">
        <el-select class="limit" size="small" :model-value="limit" @change="onLimitChange">
          <el-option
            v-for="option in LIMIT_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：无法读取案例库。启动 Python 后端后重试。"
    />
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="`案例库加载失败：${loadError}`"
    />

    <section v-if="isEmpty" class="card empty-card">
      <el-empty
        description="案例库为空：Judge 还没有产出 badcase 草稿，或数据库里没有 cases 记录。"
        :image-size="72"
      />
    </section>

    <section v-else class="card table-card">
      <el-table
        v-loading="loading"
        :data="rows"
        row-key="id"
        size="small"
        class="reviews"
        :empty-text="loadError ? '加载失败，请查看上方错误' : '没有案例'"
      >
        <el-table-column label="draft_id" min-width="180">
          <template #default="scope">
            <!-- Plain text on purpose. The legacy page linked every row to
                 /review/{draft_id}, a route that never existed — it 404'd on
                 every click. There is no review detail route in this app, so
                 do not re-add the link without adding the route first. -->
            <span class="draft mono">{{ scope.row.draft_id || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="聊天对象" min-width="130">
          <template #default="scope">
            <span class="chat">{{ scope.row.chat_name || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="96">
          <template #default="scope">
            <el-tag
              v-if="scope.row.status"
              size="small"
              :type="statusTag(scope.row.status).type"
              disable-transitions
            >
              {{ statusTag(scope.row.status).label }}
            </el-tag>
            <span v-else class="muted none">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="坏例类型" width="120">
          <template #default="scope">
            <span class="bt-type">{{ badcaseLabel(scope.row.badcase_type) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="严重度" width="104">
          <template #default="scope">
            <el-tag
              v-if="scope.row.severity"
              size="small"
              :type="severityTag(scope.row.severity).type"
              disable-transitions
            >
              {{ severityTag(scope.row.severity).label }}
            </el-tag>
            <span v-else class="muted none">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="置信度" width="80" align="right">
          <template #default="scope">
            <span class="mono num">{{ confidence(scope.row.confidence) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="总分" width="76" align="right">
          <template #default="scope">
            <span class="mono num">{{ overall(scope.row.overall_score) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="判定理由" min-width="240">
          <template #default="scope">
            <span class="reason" :title="scope.row.judge_reason || ''">
              {{ truncate(scope.row.judge_reason) }}
            </span>
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
  max-width: 82ch;
  font-size: 11px;
  line-height: 1.7;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.notice {
  width: auto;
}

.limit {
  width: 96px;
}

.empty-card {
  padding: 10px 0;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.reviews :deep(.el-table__cell) {
  padding: 7px 0;
}

.draft {
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.none {
  font-size: 12px;
}

.bt-type {
  font-size: 11.5px;
  color: var(--muted);
}

.num {
  font-size: 12px;
}

.reason {
  font-size: 11px;
  color: var(--muted);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
