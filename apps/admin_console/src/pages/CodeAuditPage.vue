<script setup lang="ts">
// The code audit worklist: every known defect in this repository, with the state
// the team keeps on it. Grouped by severity because the only question a human
// opens this page with is "what is the worst thing still open".

import { computed, onMounted, ref } from "vue";
import { Refresh, Search } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import {
  CODE_AUDIT_STATUSES,
  CODE_AUDIT_STATUS_LABELS,
  type CodeAuditIssue,
  type CodeAuditState,
  type CodeAuditStatus,
} from "@shared/types";

const DASH = "—";

type TagType = "primary" | "success" | "info" | "warning" | "danger";

// `ai_analyzing` and `failed` are written by the server while a model call is in
// flight and after it dies. Offering them would let an operator park an issue in
// a state nothing ever leaves, so the picker is restricted to the six the
// workflow actually defines transitions between.
const EDITABLE_STATUSES: readonly CodeAuditStatus[] = CODE_AUDIT_STATUSES.filter(
  (status) => status !== "ai_analyzing" && status !== "failed",
);

const STATUS_TAG: Record<CodeAuditStatus, TagType> = {
  pending: "info",
  todo: "warning",
  rethink: "primary",
  fixed: "success",
  wontfix: "info",
  deferred: "info",
  ai_analyzing: "primary",
  failed: "danger",
};

const SEVERITY_RANK: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };

/** Narrow an untyped value coming out of an Element Plus control. */
function toStatus(value: unknown): CodeAuditStatus | null {
  return CODE_AUDIT_STATUSES.find((status) => status === value) ?? null;
}

interface Group {
  severity: string;
  rows: CodeAuditIssue[];
}

const issues = ref<CodeAuditIssue[]>([]);
const states = ref<Record<string, CodeAuditState>>({});
const loading = ref(false);
const loadError = ref("");
const saveError = ref("");
const savingKey = ref("");
/** Set when the server answered 200 but could not actually read cases.db. */
const degraded = ref("");

const query = ref("");
const statusFilter = ref<CodeAuditStatus[]>([]);

/**
 * What each row's select shows. It tracks the server until a save is
 * confirmed, so a rejected write can be rolled back by writing the server's
 * value back — see changeStatus().
 */
const draft = ref<Record<string, CodeAuditStatus>>({});

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity.trim().toUpperCase()] ?? 99;
}

/** An issue with no row yet is pending; that is the server's own default. */
function statusOf(key: string): CodeAuditStatus {
  return states.value[key]?.status ?? "pending";
}

function notesOf(key: string): string {
  return states.value[key]?.notes ?? "";
}

function draftStatus(key: string): CodeAuditStatus {
  return draft.value[key] ?? statusOf(key);
}

const hasData = computed(() => issues.value.length > 0);

const statusCounts = computed<Record<CodeAuditStatus, number>>(() => {
  const counts = Object.fromEntries(CODE_AUDIT_STATUSES.map((s) => [s, 0])) as Record<
    CodeAuditStatus,
    number
  >;
  for (const issue of issues.value) counts[statusOf(issue.key)] += 1;
  return counts;
});

const visible = computed<CodeAuditIssue[]>(() => {
  const needle = query.value.trim().toLowerCase();
  return issues.value.filter((issue) => {
    if (statusFilter.value.length && !statusFilter.value.includes(statusOf(issue.key))) return false;
    if (!needle) return true;
    return [issue.key, issue.title, issue.category, issue.file, notesOf(issue.key)]
      .filter((part): part is string => typeof part === "string" && part !== "")
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
});

const groups = computed<Group[]>(() => {
  const bySeverity = new Map<string, CodeAuditIssue[]>();
  for (const issue of visible.value) {
    const bucket = bySeverity.get(issue.severity);
    if (bucket) bucket.push(issue);
    else bySeverity.set(issue.severity, [issue]);
  }
  return [...bySeverity.entries()]
    .sort((a, b) => severityRank(a[0]) - severityRank(b[0]) || a[0].localeCompare(b[0]))
    .map(([severity, list]) => ({ severity, rows: list }));
});

const isP0 = (severity: string): boolean => severityRank(severity) === 0;

function fileRef(issue: CodeAuditIssue): string {
  if (!issue.file) return "";
  return issue.line ? `${issue.file}:${issue.line}` : issue.file;
}

function preview(notes: string, max = 90): string {
  if (!notes) return "";
  const flat = notes.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const res = await api.codeAudit();
    // A read that failed is answered with 200, the static finding list, and
    // every state reset to its default — indistinguishable from a healthy list
    // unless these two extra fields are read. The response type does not
    // declare them, so the shape is widened here rather than cast to any.
    const meta = res as typeof res & { degraded?: boolean; degraded_reason?: string };
    degraded.value = meta.degraded ? meta.degraded_reason || "服务端未能读取 cases.db" : "";
    issues.value = res.issues;
    states.value = res.states ?? {};
    const next: Record<string, CodeAuditStatus> = {};
    for (const issue of res.issues) next[issue.key] = statusOf(issue.key);
    draft.value = next;
    loadError.value = "";
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

async function changeStatus(issue: CodeAuditIssue, value: unknown): Promise<void> {
  const next = toStatus(value);
  const previous = statusOf(issue.key);
  // One write at a time: every select is disabled while saving, so a second
  // pick can only reach here after the first one settled.
  if (!next || next === previous || savingKey.value) return;
  savingKey.value = issue.key;
  saveError.value = "";
  try {
    const result = await api.saveCodeAudit(issue.key, {
      status: next,
      // Notes go back with the status. The endpoint overwrites the whole record,
      // so omitting them would silently erase the review notes on a status edit.
      notes: notesOf(issue.key),
    });
    if (!result.saved) {
      throw new Error(`服务端未确认写入（返回状态 ${result.status}）`);
    }
    // Re-read instead of trusting the click: the server owns the state machine
    // and may clamp a status this page considers legal.
    await load();
  } catch (error) {
    saveError.value = `${issue.key}：状态未保存，已回到「${CODE_AUDIT_STATUS_LABELS[previous]}」。${reason(error)}`;
    // el-select only re-renders when the bound value actually changes, so the
    // revert has to be a real value change rather than re-assigning the same one.
    draft.value = { ...draft.value, [issue.key]: previous };
  } finally {
    savingKey.value = "";
  }
}

/** The checkbox group emits `unknown[]`; anything outside the enum is dropped. */
function onFilterChange(value: unknown[]): void {
  statusFilter.value = value
    .map(toStatus)
    .filter((status): status is CodeAuditStatus => status !== null);
}

function clearFilters(): void {
  statusFilter.value = [];
  query.value = "";
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">CASES · 代码审计</p>
        <h1>代码审计</h1>
        <p class="head-note muted">
          <template v-if="hasData">
            {{ issues.length }} 条已知问题 · 按严重级别分组，状态可直接修改
          </template>
          <template v-else-if="loading">读取中…</template>
          <template v-else>{{ DASH }}</template>
        </p>
      </div>
      <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <!-- Never closable: this alert is the only record that the list below it is
         not showing the real state. -->
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="loadError"
      description="清单未刷新，下面显示的是上一次成功读取的内容。"
    />
    <el-alert
      v-if="saveError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="saveError"
    />
    <!-- Every state below reads as "pending" when the server could not open
         cases.db, so this alert is the only thing standing between a broken
         database and a list that looks perfectly healthy. -->
    <el-alert
      v-if="degraded"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="状态读取失败：下列状态与备注不是真实值"
      :description="`${degraded}。问题清单本身有效，但所有状态都回落成了默认值，状态修改已停用。`"
    />

    <template v-if="hasData">
      <section class="tiles">
        <article class="card tile">
          <p class="tile-label">问题总数</p>
          <p class="tile-value">{{ issues.length }}</p>
        </article>
        <article
          v-for="status in CODE_AUDIT_STATUSES"
          :key="status"
          class="card tile"
          :class="{ zero: !statusCounts[status] }"
        >
          <p class="tile-label">
            <el-tag size="small" :type="STATUS_TAG[status]" effect="plain" disable-transitions>
              {{ CODE_AUDIT_STATUS_LABELS[status] }}
            </el-tag>
          </p>
          <p class="tile-value">{{ statusCounts[status] }}</p>
        </article>
      </section>

      <section class="card filters">
        <el-checkbox-group
          :model-value="statusFilter"
          size="small"
          class="chips"
          @update:model-value="onFilterChange"
        >
          <el-checkbox-button v-for="status in CODE_AUDIT_STATUSES" :key="status" :value="status">
            {{ CODE_AUDIT_STATUS_LABELS[status] }}
            <span class="chip-count mono">{{ statusCounts[status] }}</span>
          </el-checkbox-button>
        </el-checkbox-group>
        <div class="filters-right">
          <el-input
            v-model="query"
            :prefix-icon="Search"
            size="small"
            clearable
            placeholder="搜索 key / 标题 / 文件 / 备注"
          />
          <el-button size="small" @click="clearFilters">清除筛选</el-button>
        </div>
      </section>

      <el-empty
        v-if="!groups.length"
        description="没有符合筛选条件的条目"
        :image-size="70"
      />

      <section v-for="group in groups" :key="group.severity" class="card group">
        <p class="group-head">
          <el-tag
            size="small"
            :type="isP0(group.severity) ? 'danger' : 'info'"
            :effect="isP0(group.severity) ? 'dark' : 'plain'"
            disable-transitions
          >
            {{ group.severity || "未分级" }}
          </el-tag>
          <span class="group-count muted">{{ group.rows.length }} 条</span>
          <span v-if="isP0(group.severity)" class="group-hint">最高优先级</span>
        </p>

        <article
          v-for="issue in group.rows"
          :key="issue.key"
          class="row"
          :class="{ loud: isP0(group.severity) }"
        >
          <div class="row-main">
            <router-link
              class="row-title"
              :to="{ name: 'code-audit-detail', params: { key: encodeURIComponent(issue.key) } }"
            >
              {{ issue.title }}
            </router-link>
            <p class="row-meta mono">
              <span>{{ issue.key }}</span>
              <span v-if="issue.category" class="row-chip">{{ issue.category }}</span>
              <span v-if="fileRef(issue)" class="row-file">{{ fileRef(issue) }}</span>
            </p>
            <p v-if="preview(notesOf(issue.key))" class="row-notes" :title="notesOf(issue.key)">
              {{ preview(notesOf(issue.key)) }}
            </p>
          </div>

          <div class="row-status">
            <el-tag size="small" :type="STATUS_TAG[statusOf(issue.key)]" disable-transitions>
              {{ CODE_AUDIT_STATUS_LABELS[statusOf(issue.key)] }}
            </el-tag>
          </div>

          <el-select
            class="row-select"
            size="small"
            :model-value="draftStatus(issue.key)"
            :loading="savingKey === issue.key"
            :disabled="!!savingKey || !!degraded"
            @change="changeStatus(issue, $event)"
          >
            <el-option
              v-for="status in EDITABLE_STATUSES"
              :key="status"
              :label="CODE_AUDIT_STATUS_LABELS[status]"
              :value="status"
            />
          </el-select>
        </article>
      </section>
    </template>

    <el-skeleton v-else-if="loading" :rows="5" animated />
    <el-empty
      v-else-if="!loadError"
      description="审计清单为空：应用未随包发出问题列表"
      :image-size="70"
    />
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

.notice {
  width: auto;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
}

.tile {
  padding: 14px 16px;
}

.tile.zero {
  opacity: 0.55;
}

.tile-label {
  margin: 0 0 10px;
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.4px;
}

.tile-value {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 28px;
  line-height: 1;
}

.filters {
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}

.chips {
  display: flex;
  flex-wrap: wrap;
}

.chip-count {
  margin-left: 5px;
  font-size: 10px;
  opacity: 0.75;
}

.filters-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filters-right :deep(.el-input) {
  width: 260px;
}

.group {
  padding: 0 0 6px;
  overflow: hidden;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--canvas);
}

.group-count {
  font-size: 11px;
}

.group-hint {
  color: var(--orange);
  font-size: 10px;
  letter-spacing: 0.6px;
}

.row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 132px;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
}

.row:last-child {
  border-bottom: none;
}

.row.loud {
  border-left: 3px solid var(--orange);
}

.row-main {
  min-width: 0;
}

.row-title {
  color: var(--ink);
  font-size: 13px;
  text-decoration: none;
}

.row-title:hover {
  color: var(--green-dark);
  text-decoration: underline;
}

.row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 10px;
  flex-wrap: wrap;
}

.row-chip {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 0 5px;
}

.row-notes {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-select {
  width: 132px;
}
</style>
