<script setup lang="ts">
// Triage of the facts the memory wiki wants a human to confirm or correct.
// The list is a queue: every row is a claim a model wrote into a wiki file
// that the cleaner could not verify on its own.

import { computed, onMounted, ref } from "vue";
import { Refresh, Search } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import {
  WIKI_ACTIONS,
  WIKI_ACTION_LABELS,
  type WikiAction,
  type WikiDecision,
  type WikiReviewItem,
} from "../types";

const DASH = "—";

type Scope = "pending" | "all" | "wiki";
type TagType = "primary" | "success" | "info" | "warning" | "danger";

const ACTION_TAG: Record<WikiAction, TagType> = {
  delete: "danger",
  fix: "primary",
  mark: "warning",
  skip: "info",
};

const SCOPES: readonly { value: Scope; label: string }[] = [
  { value: "pending", label: "未决策" },
  { value: "all", label: "全部" },
  { value: "wiki", label: "按 wiki 分组" },
];

interface Group {
  wiki: string;
  isGroup: boolean;
  rows: Row[];
}

/** One table row, with the decision resolved once instead of per template read. */
interface Row {
  item: WikiReviewItem;
  decision: WikiDecision | null;
  fact: string;
  newValue: string;
}

const items = ref<WikiReviewItem[]>([]);
const decisions = ref<Record<string, WikiDecision>>({});
/** The server's own count. Shown as the headline; never recomputed from the rows. */
const serverPending = ref(0);
const loading = ref(false);
const loadError = ref("");

const query = ref("");
const scope = ref<Scope>("pending");

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

const hasData = computed(() => items.value.length > 0);

const decisionList = computed<WikiDecision[]>(() => Object.values(decisions.value));

const actionCounts = computed<Record<WikiAction, number>>(() => {
  const counts: Record<WikiAction, number> = { delete: 0, fix: 0, mark: 0, skip: 0 };
  for (const decision of decisionList.value) {
    if (decision.action in counts) counts[decision.action] += 1;
  }
  return counts;
});

function decisionOf(item: WikiReviewItem): WikiDecision | null {
  return decisions.value[item.id] ?? null;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

const filtered = computed<Row[]>(() => {
  const needle = query.value.trim().toLowerCase();
  return items.value
    .map<Row>((item) => {
      const decision = decisionOf(item);
      return {
        item,
        decision,
        fact: (item.fact || item.line || item.wiki_excerpt || "").trim(),
        newValue: decision?.new_value ?? "",
      };
    })
    .filter((row) => {
      if (scope.value === "pending" && row.decision) return false;
      if (!needle) return true;
      return [row.item.wiki, row.item.fact, row.item.line, row.item.wiki_excerpt]
        .filter((part): part is string => typeof part === "string" && part !== "")
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
});

const groups = computed<Group[]>(() => {
  if (scope.value !== "wiki") return [{ wiki: "", isGroup: false, rows: filtered.value }];
  const byWiki = new Map<string, Row[]>();
  for (const row of filtered.value) {
    const bucket = byWiki.get(row.item.wiki);
    if (bucket) bucket.push(row);
    else byWiki.set(row.item.wiki, [row]);
  }
  return [...byWiki.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([wiki, rows]) => ({ wiki, isGroup: rows.some((row) => row.item.is_group), rows }));
});

/** The radio group emits a loose value; anything outside the three options is ignored. */
function onScopeChange(value: unknown): void {
  const option = SCOPES.find((entry) => entry.value === value);
  if (option) scope.value = option.value;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const res = await api.wikiReview();
    items.value = res.items;
    decisions.value = res.decisions ?? {};
    serverPending.value = res.pending;
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
        <p class="eyebrow">CASES · WIKI 审核</p>
        <h1>Wiki 事实审核</h1>
        <p class="head-note muted">
          <template v-if="hasData">模型写入的待核实事实 · 确认后由服务端脚本写回 wiki 文件</template>
          <template v-else-if="loading">读取中…</template>
          <template v-else>{{ DASH }}</template>
        </p>
      </div>
      <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="loadError"
      :description="hasData ? '清单未刷新，下面显示的是上一次成功读取的内容。' : '没有可显示的内容。'"
    />

    <template v-if="hasData">
      <section class="tiles">
        <article class="card tile hero">
          <p class="tile-label">待决策</p>
          <p class="tile-value">{{ serverPending }}</p>
          <p class="tile-hint mono">服务端计数</p>
        </article>
        <article class="card tile">
          <p class="tile-label">条目总数</p>
          <p class="tile-value">{{ items.length }}</p>
        </article>
        <article class="card tile">
          <p class="tile-label">已决策</p>
          <p class="tile-value">{{ decisionList.length }}</p>
        </article>
        <article v-for="action in WIKI_ACTIONS" :key="action" class="card tile" :class="{ zero: !actionCounts[action] }">
          <p class="tile-label">
            <el-tag size="small" :type="ACTION_TAG[action]" effect="plain" disable-transitions>
              {{ WIKI_ACTION_LABELS[action] }}
            </el-tag>
          </p>
          <p class="tile-value">{{ actionCounts[action] }}</p>
        </article>
      </section>

      <section class="card filters">
        <el-radio-group :model-value="scope" size="small" @update:model-value="onScopeChange">
          <el-radio-button v-for="option in SCOPES" :key="option.value" :value="option.value">
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>
        <el-input
          v-model="query"
          :prefix-icon="Search"
          size="small"
          clearable
          placeholder="搜索 wiki / 事实 / 原文"
        />
      </section>

      <el-empty v-if="!groups.length" description="没有符合条件的条目" :image-size="70" />

      <section v-for="group in groups" :key="group.wiki || '__all__'" class="card group">
        <p v-if="scope === 'wiki'" class="group-head">
          <span class="group-name mono">{{ group.wiki }}</span>
          <el-tag v-if="group.isGroup" size="small" type="warning" effect="plain" disable-transitions>
            群
          </el-tag>
          <span class="muted group-count">{{ group.rows.length }} 条</span>
        </p>

        <article
          v-for="row in group.rows"
          :key="row.item.id"
          class="row"
          :class="{ decided: !!row.decision }"
        >
          <div class="row-wiki">
            <router-link
              class="row-title"
              :to="{ name: 'wiki-review-detail', params: { id: encodeURIComponent(row.item.id) } }"
            >
              {{ row.item.wiki }}
            </router-link>
            <el-tag size="small" :type="row.item.is_group ? 'warning' : 'info'" effect="plain" disable-transitions>
              {{ row.item.is_group ? "群" : "个人" }}
            </el-tag>
          </div>

          <div class="row-fact">
            <span class="fact" :title="row.fact">{{ truncate(row.fact || DASH, 110) }}</span>
            <span class="row-id mono">{{ row.item.id }}</span>
          </div>

          <div class="row-state">
            <template v-if="row.decision">
              <el-tag size="small" :type="ACTION_TAG[row.decision.action]" disable-transitions>
                {{ WIKI_ACTION_LABELS[row.decision.action] }}
              </el-tag>
              <span class="when mono">{{ row.decision.decided_at || DASH }}</span>
              <span v-if="row.newValue" class="new-value mono">→ {{ truncate(row.newValue, 40) }}</span>
            </template>
            <el-tag v-else size="small" type="info" effect="plain" disable-transitions>待确认</el-tag>
          </div>

          <router-link
            class="row-go"
            :to="{ name: 'wiki-review-detail', params: { id: encodeURIComponent(row.item.id) } }"
          >
            处理 →
          </router-link>
        </article>
      </section>
    </template>

    <el-skeleton v-else-if="loading" :rows="5" animated />
    <!-- The review file simply may not exist yet; that is an empty queue, not
         a broken endpoint, and saying "failed" would be a lie. -->
    <el-empty
      v-else-if="!loadError"
      description="暂无待审核条目：服务端还没有生成 review.json，或队列已清空"
      :image-size="80"
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
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 12px;
}

.tile {
  padding: 14px 16px;
}

.tile.hero {
  border-left: 3px solid var(--orange);
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

.tile-hint {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.filters {
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}

.filters :deep(.el-input) {
  width: 280px;
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

.group-name {
  font-size: 12px;
}

.group-count {
  font-size: 11px;
}

.row {
  display: grid;
  grid-template-columns: minmax(120px, 170px) minmax(0, 1fr) minmax(160px, auto) 68px;
  align-items: center;
  gap: 14px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--line);
}

.row:last-child {
  border-bottom: none;
}

/* A decided row is done work: it stays readable but stops competing for
   attention with what still needs a human. */
.row.decided {
  opacity: 0.55;
}

.row.decided:hover {
  opacity: 0.85;
}

.row-wiki {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.row-title {
  color: var(--ink);
  font-size: 12.5px;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-title:hover {
  color: var(--green-dark);
  text-decoration: underline;
}

.row-fact {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.fact {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-id {
  color: var(--muted);
  font-size: 10px;
  flex: none;
}

.row-state {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
  flex-wrap: wrap;
}

.when {
  color: var(--muted);
  font-size: 10px;
}

.new-value {
  color: var(--green);
  font-size: 10px;
}

.row-go {
  color: var(--green);
  font-size: 12px;
  text-align: right;
  text-decoration: none;
}

.row-go:hover {
  text-decoration: underline;
}
</style>
