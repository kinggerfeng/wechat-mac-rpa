<script setup lang="ts">
// One wiki review item: the claim, where it lives in the file, and the decision
// form. This page is the only place a review decision is written, so a decision
// that was not stored must never render as if it were.

import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Back, Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import {
  WIKI_ACTIONS,
  WIKI_ACTION_LABELS,
  type WikiAction,
  type WikiReviewDetailResponse,
} from "@shared/types";

const DASH = "—";

type TagType = "primary" | "success" | "info" | "warning" | "danger";

const ACTION_TAG: Record<WikiAction, TagType> = {
  delete: "danger",
  fix: "primary",
  mark: "warning",
  skip: "info",
};

const route = useRoute();
const router = useRouter();

const detail = ref<WikiReviewDetailResponse | null>(null);
const loading = ref(false);
const loadError = ref("");
/** 404 is a different fact from "the backend is unreachable". */
const notFound = ref(false);

const action = ref<WikiAction | "">("");
const newValue = ref("");
const saving = ref(false);
const saveError = ref("");
const saveOk = ref("");

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function paramValue(raw: unknown): string {
  const value = Array.isArray(raw) ? raw[0] : raw;
  return typeof value === "string" ? value : "";
}

/**
 * vue-router hands back the `id` segment still encoded, and the API path is
 * built from the decoded value. A hand-typed URL can carry a bare `%`, which
 * makes decodeURIComponent throw — falling back to the raw segment beats a
 * blank page.
 */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

const id = computed(() => safeDecode(paramValue(route.params.id)));

const item = computed(() => detail.value?.item ?? null);
const decision = computed(() => detail.value?.decision ?? null);

const needsValue = computed(() => action.value === "fix");
/** `fix` without a new value would store an empty correction, so it cannot ship. */
const canSubmit = computed(
  () => !!action.value && (!needsValue.value || newValue.value.trim() !== "") && !saving.value,
);

/**
 * The server sends a placeholder — `(行未找到，可能已被清洗)` or
 * `(wiki 文件不存在)` — instead of a context block. Both are parenthesised and
 * carry no line numbers, which is what tells them apart from a real excerpt.
 */
const contextMissing = computed(() => {
  const text = (detail.value?.context ?? "").trim();
  if (!text) return true;
  return text.startsWith("(") && text.endsWith(")") && !text.includes("|");
});

function syncForm(detailValue: WikiReviewDetailResponse): void {
  action.value = detailValue.decision?.action ?? "";
  newValue.value = detailValue.decision?.new_value ?? "";
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const result = await api.wikiReviewDetail(id.value);
    detail.value = result;
    notFound.value = false;
    loadError.value = "";
    syncForm(result);
  } catch (error) {
    notFound.value = error instanceof ApiError && error.status === 404;
    loadError.value = notFound.value ? "" : reason(error);
    if (notFound.value) detail.value = null;
  } finally {
    loading.value = false;
  }
}

/** The radio group emits a loose value; an unknown one clears the selection. */
function onActionUpdate(value: unknown): void {
  const found = WIKI_ACTIONS.find((entry) => entry === value);
  action.value = found ?? "";
}

async function submit(): Promise<void> {
  if (!action.value || saving.value) return;
  saving.value = true;
  saveError.value = "";
  saveOk.value = "";
  try {
    const result = await api.saveWikiReview(id.value, {
      action: action.value,
      new_value: newValue.value.trim(),
    });
    if (!result.success) {
      saveError.value = result.error || "未保存：服务端未确认写入";
      return;
    }
    saveOk.value = `已保存决策：${WIKI_ACTION_LABELS[action.value]}`;
    // Re-read so the timeline and the applied block show what the server now
    // holds, not what this page hoped it held.
    await load();
  } catch (error) {
    saveError.value = `未保存：${reason(error)}`;
  } finally {
    saving.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div class="head-left">
        <el-button size="small" text :icon="Back" @click="router.push({ name: 'wiki-review' })">
          Wiki 审核
        </el-button>
        <div>
          <p class="eyebrow">CASES · WIKI 审核</p>
          <h1 class="head-title">{{ item?.wiki || (notFound ? "条目不存在" : "读取中…") }}</h1>
        </div>
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
      :description="detail ? '以下内容为上一次成功读取的结果。' : '没有可显示的内容。'"
    />
    <el-alert
      v-if="notFound"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="服务端没有这条审核条目"
      :description="`id「${id}」不在 review.json 中，可能已被清理，或 review.json 已重新生成。`"
    />

    <template v-if="detail">
      <section class="card facts">
        <el-tag size="small" :type="item?.is_group ? 'warning' : 'info'" effect="plain" disable-transitions>
          {{ item?.is_group ? "群 wiki" : "个人 wiki" }}
        </el-tag>
        <el-tag v-if="decision" size="small" type="success" disable-transitions>已决策</el-tag>
        <el-tag v-else size="small" type="info" effect="plain" disable-transitions>未决策</el-tag>
        <span class="fact-id mono">{{ item?.id }}</span>
      </section>

      <section class="card block">
        <p class="eyebrow">涉及事实</p>
        <p class="prose">{{ item?.fact || DASH }}</p>
        <div v-if="item?.line" class="snippet">
          <p class="snippet-label muted">wiki 整行</p>
          <pre class="mono pre small">{{ item.line }}</pre>
        </div>
        <div v-if="item?.wiki_excerpt" class="snippet">
          <p class="snippet-label muted">wiki 片段</p>
          <pre class="mono pre small">{{ item.wiki_excerpt }}</pre>
        </div>
      </section>

      <section class="card block">
        <p class="eyebrow">wiki 上下文</p>
        <!-- `context` is plain text the server has already HTML-escaped and
             line-numbered. It is deliberately not v-html: re-interpreting it
             would undo that escaping and let wiki text run as markup. -->
        <pre v-if="!contextMissing" class="mono pre context">{{ detail.context }}</pre>
        <el-alert
          v-else
          class="notice"
          type="warning"
          show-icon
          :closable="false"
          title="服务端未能定位这一行的上下文"
          :description="detail.context || '（空）'"
        />
      </section>

      <section v-if="decision" class="card block applied">
        <p class="eyebrow">已应用的决策</p>
        <dl class="applied-grid">
          <dt>动作</dt>
          <dd>
            <el-tag size="small" :type="ACTION_TAG[decision.action]" disable-transitions>
              {{ WIKI_ACTION_LABELS[decision.action] }}
            </el-tag>
          </dd>
          <dt>新值</dt>
          <dd class="mono">{{ decision.new_value || DASH }}</dd>
          <dt>决策时间</dt>
          <dd class="mono">{{ decision.decided_at || DASH }}</dd>
        </dl>
        <p class="muted note">
          下方再次提交会<b>替换</b>这条决策（服务端按 id 覆盖 decisions.json 中的同一条），不会留下历史。
        </p>
      </section>

      <section class="card block">
        <p class="eyebrow">{{ decision ? "修改决策" : "提交决策" }}</p>
        <el-radio-group
          :model-value="action"
          size="small"
          @update:model-value="onActionUpdate"
        >
          <el-radio v-for="entry in WIKI_ACTIONS" :key="entry" :value="entry">
            {{ WIKI_ACTION_LABELS[entry] }}
          </el-radio>
        </el-radio-group>

        <div v-if="needsValue" class="fix-row">
          <el-input
            v-model="newValue"
            size="small"
            placeholder="修正后的正确内容"
            class="fix-input"
          />
          <p class="muted note">
            <template v-if="!newValue.trim()">
              「改为新值」必须填写内容，否则等于没有修正，服务端会存下一条空决策。
            </template>
            <template v-else>将把整行替换为：{{ newValue }}</template>
          </p>
        </div>

        <div class="submit-row">
          <el-button
            type="primary"
            size="small"
            :loading="saving"
            :disabled="!canSubmit"
            @click="submit"
          >
            提交决策
          </el-button>
          <span v-if="!action" class="muted note">请先选择一个动作。</span>
        </div>

        <el-alert
          v-if="saveError"
          class="notice"
          type="error"
          show-icon
          :closable="false"
          title="决策未保存"
          :description="saveError"
        />
        <el-alert
          v-if="saveOk"
          class="notice"
          type="success"
          show-icon
          :closable="false"
          :title="saveOk"
          description="决策仅写入 decisions.json；写回 wiki 文件需要另跑 clean_wiki_errors.py --apply-decisions。"
        />
      </section>
    </template>

    <el-skeleton v-else-if="loading" :rows="6" animated />
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
  min-width: 0;
}

.head-title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 400;
  letter-spacing: 0.5px;
}

.notice {
  width: auto;
}

.facts {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  flex-wrap: wrap;
}

.fact-id {
  color: var(--muted);
  font-size: 11px;
}

.block {
  padding: 16px 18px;
}

.prose {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.snippet {
  margin-top: 12px;
}

.snippet-label {
  margin: 0 0 4px;
  font-size: 10px;
  letter-spacing: 0.6px;
}

.pre {
  margin: 0;
  padding: 10px 12px;
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
}

/* The server's line numbering is whitespace-significant: the gutter must not
   be collapsed or wrapped, or ▶ no longer lines up with the target row. */
.pre.context {
  white-space: pre;
  overflow-x: auto;
}

.pre.small {
  max-height: 160px;
}

.applied {
  border-left: 3px solid var(--green);
}

.applied-grid {
  margin: 0;
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 8px 14px;
  align-items: center;
}

.applied-grid dt {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.6px;
}

.applied-grid dd {
  margin: 0;
  font-size: 12.5px;
  word-break: break-word;
}

.note {
  margin: 10px 0 0;
  font-size: 11px;
}

.fix-row {
  margin-top: 12px;
}

.fix-input {
  max-width: 420px;
}

.submit-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
}

.submit-row .note {
  margin: 0;
}
</style>
