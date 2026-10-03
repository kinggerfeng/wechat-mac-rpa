<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { TickFilter, TickRow } from "@shared/types";

const DASH = "—";
const PAGE_SIZES = [50, 100, 200];

/** Mirrors the legacy filter tabs; the SQL meaning lives server-side. */
const FILTERS: { value: TickFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "replied", label: "应回复" },
  { value: "skipped", label: "已跳过" },
];

type TagType = "success" | "info" | "warning" | "danger";

interface TickStatus {
  label: string;
  type: TagType;
  /** Tooltip only — the table must not grow a second line per row. */
  hint: string;
}

interface HumanLabel {
  text: string;
  type: TagType;
}

const filter = ref<TickFilter>("all");
const page = ref(1);
const size = ref(50);
const rows = ref<TickRow[]>([]);
const total = ref(0);
const loading = ref(false);
const loadError = ref("");

const emptyText = computed(() =>
  filter.value === "all"
    ? "还没有任何 tick 记录。Bot 跑完一次循环后这里才会出现。"
    : `「${filterLabel(filter.value)}」筛选下没有记录，换个筛选或回「全部」看看。`,
);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function filterLabel(value: TickFilter): string {
  return FILTERS.find((item) => item.value === value)?.label ?? String(value);
}

/**
 * `replies_sent_json` is a TEXT column holding whatever the model emitted: valid
 * JSON, `""`, `"[]"`, or occasionally a truncated fragment. A column that cannot
 * be read means "no reply recorded", never an exception and never a guess.
 */
function parseReplies(raw: string | null): string[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  return parsed.map((item) => (typeof item === "string" ? item : JSON.stringify(item)));
}

/**
 * 状态 has no column of its own: the legacy page derives it from four
 * independent columns in a fixed precedence, and the order is the meaning
 * (a skipped tick can still carry a draft in replies_sent_json).
 */
function statusOf(row: TickRow): TickStatus {
  if (row.skip_reason) {
    return { label: "跳过", type: "info", hint: row.skip_reason };
  }
  if (row.send_success) {
    return { label: "已发送", type: "success", hint: "发送成功" };
  }
  if (parseReplies(row.replies_sent_json).length) {
    return { label: "已回复(未确认)", type: "warning", hint: "有回复内容，但没有发送成功记录" };
  }
  if (row.should_reply) {
    return { label: "待回复", type: "danger", hint: pendingHint(row) };
  }
  return { label: "无动作", type: "info", hint: "既没有跳过原因，也没有回复" };
}

/** The finer read of `raw_response` the legacy page puts inside 待回复. */
function pendingHint(row: TickRow): string {
  const raw = row.raw_response ?? "";
  if (raw.startsWith("[空回复")) return "模型返回了空回复";
  if (raw.includes('"replies"')) return "模型决定不回复";
  return "应回复，但没有生成回复内容";
}

function newMessages(row: TickRow): number {
  // Legacy fallback: rows written before new_messages_count existed only fill
  // messages_count, and a stored 0 there is indistinguishable from unset.
  return row.new_messages_count || row.messages_count || 0;
}

/** judge_score is a REAL column: NULL means "not judged", which is not a 0. */
function scoreText(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : DASH;
}

function durationText(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} ms` : DASH;
}

function humanLabel(row: TickRow): HumanLabel {
  if (row.human_is_badcase === 1) {
    return {
      text: row.human_badcase_type ? `坏例 · ${row.human_badcase_type}` : "坏例",
      type: "danger",
    };
  }
  if (row.human_is_badcase === 0) return { text: "正常", type: "success" };
  return { text: DASH, type: "info" };
}

function replyFull(row: TickRow): string {
  return parseReplies(row.replies_sent_json).join("\n");
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await api.ticks({ filter: filter.value, page: page.value, size: size.value });
    // The endpoint is new; a malformed body must degrade to "no rows", not
    // blow up the template on rows.length.
    rows.value = Array.isArray(res.rows) ? res.rows : [];
    total.value = typeof res.total === "number" && Number.isFinite(res.total) ? res.total : 0;
  } catch (error) {
    // Drop the previous rows: after a filter switch they describe a different
    // query, and leaving them under an error banner reads as current data.
    rows.value = [];
    total.value = 0;
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

function onFilterChange(value: string | number | boolean | undefined): void {
  const next = FILTERS.find((item) => item.value === value);
  if (!next) return; // a value outside the three declared filters is a stale event
  filter.value = next.value;
  page.value = 1; // page 7 of a filter nobody paged to is not where to land
  void load();
}

function onSizeChange(next: number): void {
  size.value = next;
  page.value = 1;
  void load();
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">CASES · TICK 记录</p>
        <h1>Tick 记录</h1>
        <p class="head-note muted">
          来自 <span class="mono">data/cases.db</span> 的 tick_log ·
          <span class="mono">{{ filter }}</span> 筛选下共 {{ total }} 条
        </p>
      </div>
      <div class="head-actions">
        <el-radio-group
          :model-value="filter"
          size="small"
          @change="onFilterChange"
        >
          <el-radio-button
            v-for="item in FILTERS"
            :key="item.value"
            :value="item.value"
          >
            {{ item.label }}
          </el-radio-button>
        </el-radio-group>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="`读取 tick 记录失败：${loadError}`"
    >
      <template #default>下方不会显示任何数据：一次失败的查询不等于“没有记录”。</template>
    </el-alert>

    <section class="card table-card">
      <el-skeleton v-if="loading && !rows.length" class="skeleton" :rows="8" animated />

      <el-empty
        v-else-if="loadError"
        :image-size="72"
        description="未能读取 tick 记录，错误见上方。"
      />

      <el-empty v-else-if="!rows.length" :image-size="72" :description="emptyText" />

      <el-table v-else v-loading="loading" :data="rows" row-key="id" size="small" class="ticks">
        <el-table-column label="会话与 tick id" min-width="170">
          <template #default="scope">
            <router-link
              class="tick-link mono"
              :to="{ name: 'tick-detail', params: { id: scope.row.id } }"
            >
              {{ scope.row.session_id || DASH }}:#{{ scope.row.tick_id ?? DASH }}
            </router-link>
          </template>
        </el-table-column>

        <el-table-column label="聊天对象" min-width="130" show-overflow-tooltip>
          <template #default="scope">
            <span :class="{ muted: !scope.row.chat_name }">{{ scope.row.chat_name || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="新消息数" width="92" align="right">
          <template #default="scope">
            <span class="mono">{{ newMessages(scope.row) }} 条</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="140">
          <template #default="scope">
            <el-tooltip :content="statusOf(scope.row).hint" placement="top" :show-after="400">
              <el-tag size="small" :type="statusOf(scope.row).type" disable-transitions>
                {{ statusOf(scope.row).label }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="回复预览" min-width="220">
          <template #default="scope">
            <el-tooltip
              v-if="replyFull(scope.row)"
              :content="replyFull(scope.row)"
              placement="top"
              :show-after="400"
            >
              <span class="preview">{{ truncate(replyFull(scope.row), 60) }}</span>
            </el-tooltip>
            <span v-else class="muted">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="Judge 分" width="88" align="right">
          <template #default="scope">
            <span class="mono">{{ scoreText(scope.row.judge_score) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="人工标注" width="150">
          <template #default="scope">
            <el-tag
              size="small"
              :type="humanLabel(scope.row).type"
              disable-transitions
            >
              {{ humanLabel(scope.row).text }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="耗时" width="90" align="right">
          <template #default="scope">
            <span class="mono">{{ durationText(scope.row.duration_ms) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="时间" width="150">
          <template #default="scope">
            <span class="mono when" :class="{ muted: !scope.row.created_at }">
              {{ scope.row.created_at || DASH }}
            </span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!loading && !loadError && total > 0" class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="size"
          background
          small
          layout="total, sizes, prev, pager, next, jumper"
          :page-sizes="PAGE_SIZES"
          :total="total"
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
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
  flex-wrap: wrap;
}

.notice {
  width: auto;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.skeleton {
  padding: 18px;
}

.ticks :deep(.el-table__cell) {
  padding: 7px 0;
}

.tick-link {
  color: var(--green);
  font-size: 11.5px;
  text-decoration: none;
}

.tick-link:hover {
  text-decoration: underline;
}

.preview {
  display: block;
  max-width: 100%;
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.when {
  font-size: 11px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  padding: 10px 14px;
  border-top: 1px solid var(--line);
}
</style>
