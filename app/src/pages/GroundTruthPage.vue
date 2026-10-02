<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import type { GroundTruthRow } from "../types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";

/** The endpoint has no "unbounded" mode, so 全部 is a high sentinel, not a
 *  real "everything". Anything it caps is visible in the summary strip, which
 *  counts the rows that actually came back. */
const ALL_LIMIT = 5000;

const LIMIT_OPTIONS = [
  { label: "50 条", value: 50 },
  { label: "100 条", value: 100 },
  { label: "200 条", value: 200 },
  { label: "全部", value: ALL_LIMIT },
];

const FILTERS = ["all", "disagree", "unlabelled"] as const;
type GtFilter = (typeof FILTERS)[number];

/** The human-labelling vocabulary, lifted from the legacy dropdown in
 *  `scripts/admin.py`. The judge's vocabulary is a different, larger set — a
 *  human label carrying one of those falls through to the raw string rather
 *  than to a blank cell. */
const HUMAN_TYPE_LABEL: Record<string, string | undefined> = {
  hallucination: "幻觉",
  persona_break: "人设分裂",
  wrong_fact: "事实错误",
  bad_style: "风格问题",
  contradiction: "前后矛盾",
  other: "其他",
};

const engine = useEngineStore();

const rows = ref<GroundTruthRow[]>([]);
const loading = ref(false);
const loadError = ref("");
const limit = ref(200);
const filter = ref<GtFilter>("all");

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function isUnlabelled(row: GroundTruthRow): boolean {
  return row.human_is_badcase === null;
}

/**
 * An unlabelled row cannot contradict anything, so it takes precedence over
 * the server's `disagree` flag. The counts and the filter below both call this
 * pair, so the summary strip can never disagree with the table.
 */
function isDisagree(row: GroundTruthRow): boolean {
  return !isUnlabelled(row) && row.disagree === true;
}

const summary = computed(() => {
  let agree = 0;
  let disagree = 0;
  let unlabelled = 0;
  for (const row of rows.value) {
    if (isUnlabelled(row)) unlabelled += 1;
    else if (isDisagree(row)) disagree += 1;
    else agree += 1;
  }
  return { total: rows.value.length, agree, disagree, unlabelled };
});

const visibleRows = computed(() => {
  if (filter.value === "disagree") return rows.value.filter(isDisagree);
  if (filter.value === "unlabelled") return rows.value.filter(isUnlabelled);
  return rows.value;
});

/** A failure must not read as "no rows yet". */
const isEmpty = computed(
  () => !loading.value && !loadError.value && visibleRows.value.length === 0,
);

function verdict(value: number | null): { label: string; type: TagType } {
  if (value === 1) return { label: "坏例", type: "danger" };
  if (value === 0) return { label: "正常", type: "success" };
  return { label: DASH, type: "info" };
}

function consistency(row: GroundTruthRow): { label: string; type: TagType } {
  if (isUnlabelled(row)) return { label: "未标注", type: "info" };
  if (isDisagree(row)) return { label: "分歧", type: "warning" };
  return { label: "一致", type: "success" };
}

/** One decimal everywhere: the legacy page used `:.0f`, which silently turned
 *  a borderline 62.5 into 62 — and borderline scores are the whole point of
 *  this page. Null is a dash, never 0. */
function score(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return DASH;
  return value.toFixed(1);
}

function typeLabel(raw: string | null): string {
  if (!raw) return DASH;
  return HUMAN_TYPE_LABEL[raw] ?? raw;
}

function tickLabel(row: GroundTruthRow): string {
  return `${row.session_id || DASH}:#${row.tick_id ?? DASH}`;
}

function truncate(text: string | null, max = 60): string {
  if (!text) return DASH;
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function onFilterChange(value: unknown): void {
  if (FILTERS.includes(value as GtFilter)) filter.value = value as GtFilter;
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
    rows.value = (await api.groundTruth(limit.value)).rows;
  } catch (error) {
    // The previous rows are kept on purpose: blanking the table would turn a
    // transient failure into "there is nothing to see", and the alert above
    // is the only thing that would say otherwise.
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
        <p class="eyebrow">GROUND TRUTH · 真值对比</p>
        <h1>真值对比</h1>
        <p class="head-note muted">
          同一个 tick 上，LLM Judge 与人工标注结论不一致的地方。分歧行是调试 Judge 的入口：
          人工说正常而 Judge 判坏例，或反过来。数据更新于 {{ engine.lastUpdated || DASH }}。
        </p>
      </div>
      <div class="head-actions">
        <el-select
          class="limit"
          size="small"
          :model-value="limit"
          @change="onLimitChange"
        >
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
      v-if="!engine.online"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：无法读取真值对比数据。启动 Python 后端后重试。"
    />
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="`真值对比加载失败：${loadError}`"
    />

    <section class="card stats">
      <el-statistic title="已加载" :value="summary.total" />
      <el-statistic title="一致" :value="summary.agree" />
      <el-statistic title="分歧" :value="summary.disagree" :value-style="{ color: 'var(--orange)' }" />
      <el-statistic title="未标注" :value="summary.unlabelled" />
    </section>

    <div class="filter-row">
      <el-radio-group :model-value="filter" size="small" @change="onFilterChange">
        <el-radio-button value="all">全部</el-radio-button>
        <el-radio-button value="disagree">仅分歧</el-radio-button>
        <el-radio-button value="unlabelled">仅未标注</el-radio-button>
      </el-radio-group>
      <span class="muted shown">
        {{ filter === "all" ? "" : filter === "disagree" ? "仅分歧" : "仅未标注" }}
        {{ visibleRows.length }} / {{ summary.total }} 条
      </span>
    </div>

    <section v-if="isEmpty" class="card empty-card">
      <el-empty
        :description="
          filter === 'all'
            ? '还没有任何可对比的 tick：Judge 尚未给出评分。'
            : '这个筛选下没有数据：可能全部已标注且一致，或都还没标注。'
        "
        :image-size="72"
      />
    </section>

    <section v-else class="card table-card">
      <el-table
        v-loading="loading"
        :data="visibleRows"
        row-key="id"
        size="small"
        class="gt"
        :empty-text="loadError ? '加载失败，请查看上方错误' : '没有符合条件的行'"
      >
        <el-table-column label="会话 / tick" min-width="190">
          <template #default="scope">
            <router-link
              class="tick-link mono"
              :to="{ name: 'tick-detail', params: { id: scope.row.id } }"
            >
              {{ tickLabel(scope.row) }}
            </router-link>
          </template>
        </el-table-column>

        <el-table-column label="聊天对象" min-width="130">
          <template #default="scope">
            <span class="chat">{{ scope.row.chat_name || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="Judge 判定" width="94">
          <template #default="scope">
            <el-tag size="small" :type="verdict(scope.row.judge_is_badcase).type" disable-transitions>
              {{ verdict(scope.row.judge_is_badcase).label }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="人工判定" width="94">
          <template #default="scope">
            <el-tag
              v-if="scope.row.human_is_badcase !== null"
              size="small"
              :type="verdict(scope.row.human_is_badcase).type"
              disable-transitions
            >
              {{ verdict(scope.row.human_is_badcase).label }}
            </el-tag>
            <span v-else class="muted none">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="Judge 分" width="86" align="right">
          <template #default="scope">
            <span class="mono score">{{ score(scope.row.judge_score) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="坏例类型" width="104">
          <template #default="scope">
            <span class="bt-type">{{ typeLabel(scope.row.human_badcase_type) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="模型原始输出" min-width="240">
          <template #default="scope">
            <span class="raw" :title="scope.row.raw_response || ''">
              {{ truncate(scope.row.raw_response) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="一致性" width="88">
          <template #default="scope">
            <el-tag size="small" :type="consistency(scope.row).type" disable-transitions>
              {{ consistency(scope.row).label }}
            </el-tag>
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

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  padding: 14px 18px;
}

.stats :deep(.el-statistic__head) {
  font-size: 10px;
  color: var(--muted);
  margin-bottom: 4px;
}

.stats :deep(.el-statistic__content) {
  font-size: 22px;
  font-family: var(--font-mono);
  color: var(--ink);
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.shown {
  font-size: 10.5px;
  font-family: var(--font-mono);
}

.empty-card {
  padding: 10px 0;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.gt :deep(.el-table__cell) {
  padding: 7px 0;
}

.tick-link {
  font-size: 11px;
  color: var(--path-element);
  text-decoration: none;
}

.tick-link:hover {
  text-decoration: underline;
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

.score {
  font-size: 12px;
}

.bt-type {
  font-size: 11px;
  color: var(--muted);
}

.raw {
  font-size: 11px;
  color: var(--muted);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
