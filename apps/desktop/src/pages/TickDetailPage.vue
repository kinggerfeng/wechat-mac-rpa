<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ArrowLeft, DocumentCopy, Refresh } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";

import { api, ApiError } from "../api/client";
import type { TickDetail } from "../types";

const DASH = "—";

/** The type vocabulary the legacy admin form offered; inventing new values
 *  here would split the counts on the 真值对比 page. */
const BADCASE_TYPES = [
  { value: "hallucination", label: "幻觉" },
  { value: "persona_break", label: "人设分裂" },
  { value: "wrong_fact", label: "事实错误" },
  { value: "bad_style", label: "风格问题" },
  { value: "contradiction", label: "前后矛盾" },
  { value: "other", label: "其他" },
];

type Verdict = "normal" | "bad" | "clear";
type TagType = "success" | "info" | "warning" | "danger";

interface TickStatus {
  label: string;
  type: TagType;
  hint: string;
}

interface JsonBlock {
  text: string;
  /** False means the column was not JSON and the raw string is shown instead. */
  parsed: boolean;
}

interface DimRow {
  key: string;
  value: string;
}

const route = useRoute();
const router = useRouter();

const tick = ref<TickDetail | null>(null);
const loading = ref(false);
const loadError = ref("");
const missing = ref(false);
const imageBroken = ref(false);

const saving = ref(false);
const saveError = ref("");
const verdict = ref<Verdict | null>(null);
const badcaseType = ref("");
const notes = ref("");
const typeError = ref("");

const tab = ref("overview");
const copied = ref("");

let copyTimer: number | null = null;

/** Route params are strings; anything that is not a positive integer is a bad
 *  link and must not be turned into a request for id 0. */
const tickId = computed<number | null>(() => {
  const raw = route.params.id;
  const text = Array.isArray(raw) ? raw[0] : raw;
  if (typeof text !== "string" || !/^\d+$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
});

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/**
 * Duplicated from TickListPage on purpose: the two pages are the only
 * consumers and a shared module would mean a third file outside this change.
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

function statusOf(row: TickDetail): TickStatus {
  if (row.skip_reason) return { label: "跳过", type: "info", hint: row.skip_reason };
  if (row.send_success) return { label: "已发送", type: "success", hint: "发送成功" };
  if (parseReplies(row.replies_sent_json).length) {
    return { label: "已回复(未确认)", type: "warning", hint: "有回复内容，但没有发送成功记录" };
  }
  if (row.should_reply) return { label: "待回复", type: "danger", hint: pendingHint(row) };
  return { label: "无动作", type: "info", hint: "既没有跳过原因，也没有回复" };
}

function pendingHint(row: TickDetail): string {
  const raw = row.raw_response ?? "";
  if (raw.startsWith("[空回复")) return "模型返回了空回复";
  if (raw.includes('"replies"')) return "模型决定不回复";
  return "应回复，但没有生成回复内容";
}

function newMessages(row: TickDetail): number {
  return row.new_messages_count || row.messages_count || 0;
}

function scoreText(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : DASH;
}

function durationText(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} ms` : DASH;
}

function verdictText(value: number | null): string {
  if (value === 1) return "坏例";
  if (value === 0) return "正常";
  return "未评分";
}

/** Anything that is not exactly 0 or 1 is "not labelled" — including a null
 *  and a key the server did not send at all. */
function humanText(row: TickDetail): { text: string; muted: boolean } {
  if (row.human_is_badcase === 1) {
    return {
      text: row.human_badcase_type ? `坏例 · ${row.human_badcase_type}` : "坏例",
      muted: false,
    };
  }
  if (row.human_is_badcase === 0) return { text: "正常", muted: false };
  return { text: "未标注", muted: true };
}

/** Pretty-print when the column really is JSON, otherwise hand back the raw
 *  string and let the template say so — an empty box reads as "nothing was
 *  produced", which is a different claim. */
function jsonBlock(raw: string | null, empty: string): JsonBlock {
  if (!raw || !raw.trim()) return { text: empty, parsed: false };
  try {
    return { text: JSON.stringify(JSON.parse(raw), null, 2), parsed: true };
  } catch {
    return { text: raw, parsed: false };
  }
}

const repliesBlock = computed<JsonBlock>(() =>
  jsonBlock(tick.value?.replies_sent_json ?? null, "没有回复记录。"),
);
const toolCallsBlock = computed<JsonBlock>(() =>
  jsonBlock(tick.value?.tool_calls_json ?? null, "没有工具调用记录。"),
);

function dimValue(value: unknown): string {
  if (value === null || value === undefined) return DASH;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (typeof value === "object") {
    const record = value as { score?: unknown; comment?: unknown };
    const parts: string[] = [];
    if (record.score !== undefined && record.score !== null) parts.push(`${String(record.score)}/100`);
    if (typeof record.comment === "string" && record.comment) parts.push(record.comment);
    if (parts.length) return parts.join(" · ");
    try {
      return JSON.stringify(value);
    } catch {
      return DASH;
    }
  }
  return String(value);
}

const dimensions = computed<DimRow[] | null>(() => {
  const row = tick.value;
  if (!row) return null;
  const raw = row.judge_dimensions_json;
  if (!raw || !raw.trim()) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;
  if (Array.isArray(parsed)) {
    return parsed.map((item, index) => ({
      key: dimName(item) ?? `#${index + 1}`,
      value: dimValue(item),
    }));
  }
  return Object.entries(parsed as Record<string, unknown>).map(([key, value]) => ({
    key,
    value: dimValue(value),
  }));
});

function dimName(item: unknown): string | null {
  if (item && typeof item === "object") {
    const name = (item as { name?: unknown }).name;
    if (typeof name === "string" && name) return name;
  }
  return null;
}

const screenshotName = computed<string | null>(() => {
  const path = tick.value?.screenshot_path;
  if (!path) return null;
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
});

const screenshotUrl = computed<string | null>(() => {
  const name = screenshotName.value;
  return name ? api.screenshotImageUrl(name) : null;
});

/** The screenshot endpoints are keyed by tick_id; a row without one has no
 *  reachable detail page, and linking to the row id would be a guess. */
const screenshotLinkId = computed<number | null>(() => {
  const value = tick.value?.tick_id;
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
});

const badcaseTypeOptions = computed(() => {
  const current = badcaseType.value;
  // A stored type outside the vocabulary is still the operator's own label;
  // dropping it from the select would silently rewrite it on the next save.
  const extra =
    current && !BADCASE_TYPES.some((item) => item.value === current)
      ? [{ value: current, label: `${current}（历史值）` }]
      : [];
  return [...BADCASE_TYPES, ...extra];
});

const canSave = computed(() => {
  if (saving.value) return false;
  if (verdict.value === "bad") return badcaseType.value !== "";
  return verdict.value !== null;
});

function prefill(row: TickDetail): void {
  // An unlabelled row has no truthful radio, so none is pre-selected.
  if (row.human_is_badcase === 1) verdict.value = "bad";
  else if (row.human_is_badcase === 0) verdict.value = "normal";
  else verdict.value = null;
  badcaseType.value = row.human_badcase_type ?? "";
  notes.value = row.human_notes ?? "";
  typeError.value = "";
  saveError.value = "";
}

async function load(): Promise<void> {
  const id = tickId.value;
  if (id === null) {
    tick.value = null;
    return;
  }
  loading.value = true;
  loadError.value = "";
  missing.value = false;
  imageBroken.value = false;
  try {
    const row = await api.tick(id);
    if (!row) {
      // A 200 with no row is a deleted tick, not a blank page.
      missing.value = true;
      return;
    }
    tick.value = row;
    prefill(row);
  } catch (error) {
    tick.value = null;
    // A deleted tick and a dead backend need different words: one is gone for
    // good, the other comes back on its own.
    if (error instanceof ApiError && error.status === 404) missing.value = true;
    else loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  const id = tickId.value;
  if (id === null || !tick.value) return;
  if (verdict.value === "bad" && !badcaseType.value) {
    typeError.value = "「坏例」必须给出类型，否则这条标注无法统计。";
    return;
  }
  if (!canSave.value) return;
  saving.value = true;
  saveError.value = "";
  const clearing = verdict.value === "clear";
  try {
    if (clearing) {
      // A clear is a DELETE, not a save of "normal": the first withdraws the
      // verdict, the second writes a verdict the reviewer did not make.
      await api.clearGroundTruth(id);
    } else {
      await api.saveGroundTruth(id, {
        is_badcase: verdict.value === "bad",
        badcase_type: verdict.value === "bad" ? badcaseType.value : "",
        notes: notes.value,
      });
    }
  } catch (error) {
    // Keep whatever was typed: losing the note on a failed save is how a
    // human label quietly disappears.
    saveError.value = reason(error);
    saving.value = false;
    return;
  }
  saving.value = false;
  ElMessage.success(clearing ? "标注已清除" : "标注已保存");
  // The refresh is deliberately outside the failure branch: human_labeled_at is
  // written by the server, and a reload that fails is not a failed save.
  await load();
}

function back(): void {
  void router.push({ name: "ticks" });
}

function onVerdictChange(value: string | number | boolean | undefined): void {
  verdict.value =
    value === "normal" || value === "bad" || value === "clear" ? value : null;
  typeError.value = "";
  saveError.value = "";
}

async function copy(key: string, text: string): Promise<void> {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    copied.value = key;
  } catch {
    ElMessage.warning("复制失败：剪贴板不可用");
    return;
  }
  if (copyTimer !== null) window.clearTimeout(copyTimer);
  copyTimer = window.setTimeout(() => {
    copied.value = "";
    copyTimer = null;
  }, 1600);
}

watch(tickId, () => void load(), { immediate: true });

onUnmounted(() => {
  if (copyTimer !== null) window.clearTimeout(copyTimer);
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">CASES · TICK 详情</p>
        <h1 class="tick-title mono">
          {{ tick ? `${tick.session_id || DASH}:#${tick.tick_id ?? DASH}` : (tickId === null ? "链接无效" : "载入中") }}
        </h1>
        <p v-if="tick" class="head-note muted">
          行 id <span class="mono">{{ tick.id }}</span> ·
          {{ tick.chat_name || "未命名会话" }} ·
          {{ tick.created_at || DASH }}
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="ArrowLeft" size="small" @click="back">
          返回列表
        </el-button>
        <el-button
          :icon="Refresh"
          size="small"
          :loading="loading"
          :disabled="tickId === null"
          @click="load"
        >
          刷新
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="tickId === null"
      type="error"
      show-icon
      :closable="false"
      title="链接无效"
    >
      <template #default>
        路由参数 <span class="mono">{{ route.params.id }}</span> 不是一条 tick 的正整数 id，未向服务端发起请求。
      </template>
    </el-alert>

    <el-alert
      v-else-if="missing"
      type="warning"
      show-icon
      :closable="false"
      title="这条 tick 已不存在（404）"
    >
      <template #default>它可能已被 cases.db 的清理脚本删除；返回列表查看现有记录。</template>
    </el-alert>

    <el-alert
      v-else-if="loadError"
      type="error"
      show-icon
      :closable="false"
      :title="`读取 tick 失败：${loadError}`"
    />

    <el-card v-if="tick" shadow="never" class="body">
      <el-tabs v-model="tab">
        <!-- 概览 -->
        <el-tab-pane label="概览" name="overview">
          <div class="status-row">
            <el-tag :type="statusOf(tick).type" disable-transitions>{{ statusOf(tick).label }}</el-tag>
            <span class="muted status-hint">{{ statusOf(tick).hint }}</span>
          </div>

          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="会话 id">
              <span class="mono">{{ tick.session_id || DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="tick id">
              <span class="mono">{{ tick.tick_id ?? DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="聊天对象">{{ tick.chat_name || DASH }}</el-descriptions-item>
            <el-descriptions-item label="时间">
              <span class="mono">{{ tick.created_at || DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="耗时">{{ durationText(tick.duration_ms) }}</el-descriptions-item>
            <el-descriptions-item label="新消息数">
              <span class="mono">{{ newMessages(tick) }} 条</span>
              <span class="muted note-inline">（消息总数 {{ tick.messages_count ?? DASH }}）</span>
            </el-descriptions-item>
            <el-descriptions-item label="should_reply / send_success">
              <span class="mono">{{ tick.should_reply ?? DASH }} / {{ tick.send_success ?? DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="跳过原因">
              <span :class="{ muted: !tick.skip_reason }">{{ tick.skip_reason || DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="Judge 分">{{ scoreText(tick.judge_score) }}</el-descriptions-item>
            <el-descriptions-item label="Judge 判定">{{ verdictText(tick.judge_is_badcase) }}</el-descriptions-item>
            <el-descriptions-item label="Judge 理由" :span="2">
              <span :class="{ muted: !tick.judge_reason }">{{ tick.judge_reason || DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="人工标注" :span="2">
              <span :class="{ muted: humanText(tick).muted }">{{ humanText(tick).text }}</span>
              <span class="muted note-inline">
                标注于 {{ tick.human_labeled_at || DASH }}
              </span>
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>

        <!-- 模型输出 -->
        <el-tab-pane label="模型输出" name="output">
          <section class="block-group">
            <header class="block-head">
              <h3>raw_response</h3>
            </header>
            <pre v-if="tick.raw_response" class="block mono">{{ tick.raw_response }}</pre>
            <p v-else class="muted block-empty">这一 tick 没有留下 raw_response。</p>
          </section>

          <section class="block-group">
            <header class="block-head">
              <h3>replies_sent_json</h3>
              <el-button
                v-if="tick.replies_sent_json"
                :icon="DocumentCopy"
                size="small"
                @click="copy('replies', repliesBlock.text)"
              >
                {{ copied === "replies" ? "已复制" : "复制" }}
              </el-button>
            </header>
            <pre class="block mono">{{ repliesBlock.text }}</pre>
            <p v-if="tick.replies_sent_json && !repliesBlock.parsed" class="unparsed">
              该列不是合法 JSON，以下为原始字符串。
            </p>
          </section>
        </el-tab-pane>

        <!-- Prompt -->
        <el-tab-pane label="Prompt" name="prompt">
          <section class="block-group">
            <header class="block-head">
              <h3>system_prompt</h3>
              <el-button
                :icon="DocumentCopy"
                size="small"
                :disabled="!tick.system_prompt"
                @click="copy('system', tick.system_prompt ?? '')"
              >
                {{ copied === "system" ? "已复制" : "复制" }}
              </el-button>
            </header>
            <pre v-if="tick.system_prompt" class="block mono prompt">{{ tick.system_prompt }}</pre>
            <p v-else class="muted block-empty">没有记录 system_prompt。</p>
          </section>

          <section class="block-group">
            <header class="block-head">
              <h3>user_prompt</h3>
              <el-button
                :icon="DocumentCopy"
                size="small"
                :disabled="!tick.user_prompt"
                @click="copy('user', tick.user_prompt ?? '')"
              >
                {{ copied === "user" ? "已复制" : "复制" }}
              </el-button>
            </header>
            <pre v-if="tick.user_prompt" class="block mono prompt">{{ tick.user_prompt }}</pre>
            <p v-else class="muted block-empty">没有记录 user_prompt。</p>
          </section>

          <section class="block-group">
            <header class="block-head">
              <h3>tool_calls_json</h3>
              <el-button
                :icon="DocumentCopy"
                size="small"
                :disabled="!tick.tool_calls_json"
                @click="copy('tools', toolCallsBlock.text)"
              >
                {{ copied === "tools" ? "已复制" : "复制" }}
              </el-button>
            </header>
            <pre class="block mono">{{ toolCallsBlock.text }}</pre>
            <p v-if="tick.tool_calls_json && !toolCallsBlock.parsed" class="unparsed">
              该列不是合法 JSON，以下为原始字符串。
            </p>
          </section>
        </el-tab-pane>

        <!-- 评分维度 -->
        <el-tab-pane label="评分维度" name="dims">
          <el-descriptions v-if="dimensions && dimensions.length" :column="1" border size="small">
            <el-descriptions-item v-for="row in dimensions" :key="row.key" :label="row.key">
              <span class="mono">{{ row.value }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else :image-size="64" description="无维度数据" />
        </el-tab-pane>

        <!-- 人工标注 -->
        <el-tab-pane label="人工标注" name="human">
          <el-alert
            v-if="saveError"
            class="notice"
            type="error"
            show-icon
            :closable="false"
            :title="`保存失败：${saveError}`"
          >
            <template #default>表单内容已保留，修正后可再次提交。</template>
          </el-alert>

          <el-alert
            v-if="verdict === 'clear'"
            class="notice"
            type="info"
            show-icon
            :closable="false"
            title="保存后会清除这条标注"
          >
            <template #default>
              标注、类型、备注和时间戳会一并置空，回到「未标注」。
              这与保存「正常」不同——那是一条判定，而这是没有判定。
            </template>
          </el-alert>

          <!-- No `model` on purpose: the fields are plain refs (an unlabelled
               tick must be able to show *no* selected radio) and the one
               required field is validated by the explicit :error below. -->
          <el-form label-width="90px" class="gt-form">
            <el-form-item label="判定">
              <el-radio-group :model-value="verdict ?? undefined" @change="onVerdictChange">
                <el-radio value="normal">正常</el-radio>
                <el-radio value="bad">坏例</el-radio>
                <el-radio value="clear">清除标注</el-radio>
              </el-radio-group>
              <span v-if="tick.human_labeled_at" class="muted note-inline">
                上次标注：{{ tick.human_labeled_at }}
              </span>
            </el-form-item>

            <el-form-item
              v-if="verdict === 'bad'"
              label="坏例类型"
              required
              :error="typeError"
            >
              <el-select
                v-model="badcaseType"
                class="type-select"
                placeholder="选择类型"
                filterable
                default-first-option
              >
                <el-option
                  v-for="item in badcaseTypeOptions"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="点评">
              <el-input
                v-model="notes"
                type="textarea"
                :rows="3"
                placeholder="补充说明（可选）"
              />
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :loading="saving"
                :disabled="!canSave"
                @click="save"
              >
                保存标注
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- 截图 -->
        <el-tab-pane label="截图" name="shot">
          <div v-if="screenshotUrl && !imageBroken" class="shot">
            <img
              class="shot-img"
              :src="screenshotUrl"
              :alt="`tick ${tick.id} 截图`"
              @error="imageBroken = true"
            />
            <p class="mono muted shot-path">{{ tick.screenshot_path }}</p>
          </div>
          <el-empty
            v-else-if="imageBroken"
            :image-size="64"
            description="截图不可用"
          >
            <p class="muted shot-note">
              文件 <span class="mono">{{ screenshotName || DASH }}</span> 读取失败，可能已被清理或移动。
            </p>
          </el-empty>
          <el-empty v-else :image-size="64" description="这一 tick 没有截图。" />
          <p v-if="screenshotLinkId" class="shot-link">
            <router-link
              class="mono"
              :to="{ name: 'screenshot-detail', params: { tickId: screenshotLinkId } }"
            >
              查看截图详情 →
            </router-link>
          </p>
          <p v-else-if="screenshotUrl" class="muted shot-note">
            该记录没有 tick 号，无法打开截图详情页。
          </p>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-skeleton v-else-if="loading" class="card skeleton" :rows="8" animated />
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
  font-size: 24px;
  font-weight: 400;
  letter-spacing: 0.5px;
}

.tick-title {
  font-size: 22px;
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

.body :deep(.el-card__body) {
  padding-top: 6px;
}

.status-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.status-hint {
  font-size: 11px;
}

.note-inline {
  margin-left: 8px;
  font-size: 11px;
}

.block-group {
  margin-bottom: 20px;
}

.block-group:last-child {
  margin-bottom: 0;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.block-head h3 {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.4px;
}

.block {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--canvas);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 420px;
  overflow: auto;
}

.block.prompt {
  max-height: 320px;
}

.block-empty,
.unparsed {
  margin: 0;
  font-size: 11.5px;
}

.unparsed {
  margin-top: 6px;
  color: var(--orange);
}

.gt-form {
  max-width: 640px;
}

.type-select {
  width: 240px;
}

.notice {
  margin-bottom: 12px;
}

.skeleton {
  padding: 18px;
}

.shot-img {
  max-width: 100%;
  max-height: 560px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  display: block;
}

.shot-path,
.shot-link {
  margin: 8px 0 0;
  font-size: 11px;
}

.shot-note {
  margin: 0;
  font-size: 11px;
}
</style>
