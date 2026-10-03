<script setup lang="ts">
// One audit issue: its state, its notes, and every round of the human ↔ model
// loop. The loop is the whole point of this page — a proposal is only useful
// next to the requirement that produced it.

import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Back, Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import {
  CODE_AUDIT_STATUSES,
  CODE_AUDIT_STATUS_LABELS,
  type CodeAuditDetailResponse,
  type CodeAuditRound,
  type CodeAuditStatus,
} from "@shared/types";

const DASH = "—";

type TagType = "primary" | "success" | "info" | "warning" | "danger";

// Server-owned states: the model enters them, the server leaves them. Offering
// them here would strand the issue in a state no operator action can clear.
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

const route = useRoute();
const router = useRouter();

const detail = ref<CodeAuditDetailResponse | null>(null);
const loading = ref(false);
const loadError = ref("");
/** 404 is a different fact from "the backend is unreachable". */
const notFound = ref(false);
/** Set when the server answered 200 but could not actually read cases.db. */
const degraded = ref("");

const requirement = ref("");
const analyzing = ref(false);
const analyzeError = ref("");
const analyzeReply = ref("");
const analyzeRound = ref(0);
const elapsed = ref(0);

const formStatus = ref<CodeAuditStatus>("pending");
const formNotes = ref("");
const formDirty = ref(false);
const saving = ref(false);
const saveError = ref("");
const saveOk = ref("");

const actionBusy = ref<"" | "execute" | "reject">("");
const actionError = ref("");

let ticker: number | null = null;

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function paramValue(raw: unknown): string {
  const value = Array.isArray(raw) ? raw[0] : raw;
  return typeof value === "string" ? value : "";
}

/**
 * vue-router encodes the `key` segment when the link is built and hands it back
 * still encoded, so it has to be decoded before the API sees it. A hand-typed
 * URL can carry a bare `%`, which makes decodeURIComponent throw — falling back
 * to the raw segment beats rendering a blank page.
 */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

// The server matches the key exactly, so nothing here may normalise it.
const key = computed(() => safeDecode(paramValue(route.params.key)));

const isP0 = computed(() => (detail.value?.issue.severity ?? "").trim().toUpperCase() === "P0");

const fileRef = computed(() => {
  const issue = detail.value?.issue;
  if (!issue?.file) return "";
  return issue.line ? `${issue.file}:${issue.line}` : issue.file;
});

/** The server returns rounds newest-first; sorting guards the ordering anyway. */
const rounds = computed<CodeAuditRound[]>(() =>
  [...(detail.value?.rounds ?? [])].sort((a, b) => b.round - a.round),
);

function startTicker(): void {
  elapsed.value = 0;
  stopTicker();
  ticker = window.setInterval(() => {
    elapsed.value += 1;
  }, 1000);
}

function stopTicker(): void {
  if (ticker !== null) {
    window.clearInterval(ticker);
    ticker = null;
  }
}

function syncForm(detailValue: CodeAuditDetailResponse): void {
  formStatus.value = detailValue.status;
  formNotes.value = detailValue.notes;
  formDirty.value = false;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const result = await api.codeAuditDetail(key.value);
    // A read that failed is answered with 200 and every field reset to its
    // default — indistinguishable from an untouched issue unless these two
    // extra fields are read. The response type does not declare them, so the
    // shape is widened here rather than cast to any.
    const meta = result as typeof result & { degraded?: boolean; degraded_reason?: string };
    degraded.value = meta.degraded ? meta.degraded_reason || "服务端未能读取 cases.db" : "";
    detail.value = result;
    notFound.value = false;
    loadError.value = "";
    // Never overwrite notes the operator is still typing, and never seed the
    // form from a degraded read: saving the empty defaults back would erase
    // the very notes the read failed to return.
    if (!formDirty.value && !degraded.value) syncForm(result);
  } catch (error) {
    notFound.value = error instanceof ApiError && error.status === 404;
    loadError.value = notFound.value ? "" : reason(error);
    if (notFound.value) detail.value = null;
  } finally {
    loading.value = false;
  }
}

async function runAnalyze(): Promise<void> {
  const text = requirement.value.trim();
  if (!text || analyzing.value) return;
  analyzing.value = true;
  analyzeError.value = "";
  analyzeReply.value = "";
  analyzeRound.value = 0;
  startTicker();
  try {
    const result = await api.analyzeCodeAudit(key.value, text);
    analyzeRound.value = result.round;
    if (result.success) {
      analyzeReply.value = result.reply;
      // The box held the requirement that is now archived in the round history.
      requirement.value = "";
    } else {
      // The round row is already durable server-side, so the page re-reads it —
      // but the text stays in the box, because it is the only copy of the ask.
      analyzeError.value = `${result.error || "服务端未给出原因"}。该轮记录已落库、方案为空；要求已保留在下方输入框。`;
    }
    await load();
  } catch (error) {
    // A transport failure is not proof that nothing ran: the server writes the
    // round and flips the status before it calls the model, so re-read rather
    // than leave a status that may already have moved.
    analyzeError.value = `${reason(error)}。无法确认服务端是否已记录该轮；要求已保留在下方输入框。`;
    await load();
  } finally {
    analyzing.value = false;
    stopTicker();
  }
}

async function runAction(kind: "execute" | "reject"): Promise<void> {
  if (actionBusy.value) return;
  const label = kind === "execute" ? "标记为已认领" : "退回";
  const nextStatus = kind === "execute" ? "todo" : "pending";
  try {
    await ElMessageBox.confirm(
      kind === "execute"
        ? "确认已认可 AI 方案？状态将变为「已认领」，由人工开始修代码。"
        : "退回后状态回到「待处理」，当前要求与方案保留。",
      label,
      { type: "warning", confirmButtonText: "确定", cancelButtonText: "取消" },
    );
  } catch {
    return; // the operator closed the dialog
  }
  actionBusy.value = kind;
  actionError.value = "";
  try {
    const result =
      kind === "execute"
        ? await api.executeCodeAudit(key.value)
        : await api.rejectCodeAudit(key.value);
    if (!result.success) {
      actionError.value = `${label}未生效：服务端返回失败`;
      return;
    }
    ElMessage.success(`${label}成功 · 状态 → ${CODE_AUDIT_STATUS_LABELS[nextStatus]}`);
    await load();
  } catch (error) {
    actionError.value = `${label}失败：${reason(error)}`;
  } finally {
    actionBusy.value = "";
  }
}

async function save(): Promise<void> {
  if (saving.value || degraded.value) return;
  saving.value = true;
  saveError.value = "";
  saveOk.value = "";
  try {
    const result = await api.saveCodeAudit(key.value, {
      status: formStatus.value,
      notes: formNotes.value,
    });
    if (!result.saved) {
      saveError.value = `未保存：服务端未确认写入（返回状态 ${result.status}）`;
      return;
    }
    saveOk.value = `已保存 · 状态 ${CODE_AUDIT_STATUS_LABELS[result.status]}`;
    formDirty.value = false;
    await load();
  } catch (error) {
    saveError.value = `未保存：${reason(error)}`;
  } finally {
    saving.value = false;
  }
}

onMounted(() => void load());
onUnmounted(stopTicker);
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div class="head-left">
        <el-button size="small" text :icon="Back" @click="router.push({ name: 'code-audit' })">
          审计列表
        </el-button>
        <div>
          <p class="eyebrow">CASES · 代码审计</p>
          <h1 class="head-title">{{ detail?.issue.title || (notFound ? "条目不存在" : "读取中…") }}</h1>
        </div>
      </div>
      <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <!-- Not closable: an operator must not be able to dismiss the one signal
         that what follows is not the server's current answer. -->
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
      title="服务端没有这条审计记录"
      :description="`key「${key}」不在随包发出的问题清单中，可能拼写有误或该问题已移除。`"
    />
    <el-alert
      v-if="actionError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="actionError"
    />
    <!-- A degraded read returns status "pending" and empty notes with HTTP 200.
         Writing those defaults back would destroy the state it failed to
         read, so the form stays closed until the database answers. -->
    <el-alert
      v-if="degraded"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="状态读取失败：当前展示的状态、备注与方案不是真实值"
      :description="`${degraded}。状态与备注的保存已停用。`"
    />

    <template v-if="detail">
      <section class="card facts">
        <el-tag
          size="small"
          :type="isP0 ? 'danger' : 'info'"
          :effect="isP0 ? 'dark' : 'plain'"
          disable-transitions
        >
          {{ detail.issue.severity || "未分级" }}
        </el-tag>
        <el-tag size="small" :type="STATUS_TAG[detail.status]" disable-transitions>
          {{ CODE_AUDIT_STATUS_LABELS[detail.status] }}
        </el-tag>
        <span v-if="detail.issue.category" class="fact-chip">{{ detail.issue.category }}</span>
        <span v-if="fileRef" class="fact-file mono">{{ fileRef }}</span>
        <span class="fact-key mono">{{ detail.issue.key }}</span>
      </section>

      <section v-if="detail.issue.description" class="card block">
        <p class="eyebrow">问题描述</p>
        <p class="prose">{{ detail.issue.description }}</p>
      </section>

      <section class="split">
        <div class="card block">
          <p class="eyebrow">当前要求 NOTES</p>
          <pre v-if="detail.notes" class="mono pre">{{ detail.notes }}</pre>
          <p v-else class="muted none">（空）</p>
        </div>
        <div class="card block">
          <p class="eyebrow">当前方案 AI PROPOSAL</p>
          <pre v-if="detail.ai_proposal" class="mono pre">{{ detail.ai_proposal }}</pre>
          <p v-else class="muted none">（空）</p>
        </div>
      </section>

      <section class="card block">
        <div class="block-head">
          <p class="eyebrow">交给模型分析</p>
          <div class="head-actions">
            <el-button
              size="small"
              type="warning"
              :loading="actionBusy === 'execute'"
              :disabled="!!actionBusy"
              @click="runAction('execute')"
            >
              标记为已认领
            </el-button>
            <el-button
              size="small"
              :loading="actionBusy === 'reject'"
              :disabled="!!actionBusy"
              @click="runAction('reject')"
            >
              退回
            </el-button>
          </div>
        </div>

        <el-input
          v-model="requirement"
          type="textarea"
          :rows="4"
          :disabled="analyzing"
          placeholder="写下给模型的要求，例如：时间戳解析不要 hardcode 格式，要支持相对时间"
        />

        <div class="analyze-bar">
          <el-button
            type="primary"
            size="small"
            :loading="analyzing"
            :disabled="analyzing || !requirement.trim()"
            @click="runAnalyze"
          >
            交给模型分析
          </el-button>
          <span class="muted hint">服务端最长等待 60 秒；要求会先落库再调用模型。</span>
        </div>

        <div v-if="analyzing" class="analyze-progress">
          <el-progress :percentage="100" :indeterminate="true" :duration="2.5" :stroke-width="6" />
          <p class="muted hint">
            模型分析中 · 已等待 {{ elapsed }} 秒。分析期间状态为「分析中」，切走页面不会中断。
          </p>
        </div>

        <el-alert
          v-if="analyzeError"
          class="notice"
          type="error"
          show-icon
          :closable="false"
          title="分析未成功"
          :description="analyzeError"
        />
        <div v-if="analyzeReply" class="reply">
          <p class="eyebrow">第 {{ analyzeRound }} 轮回复</p>
          <pre class="mono pre">{{ analyzeReply }}</pre>
        </div>
      </section>

      <section class="card block">
        <p class="eyebrow">状态与备注</p>
        <el-alert
          v-if="degraded"
          class="notice"
          type="warning"
          show-icon
          :closable="false"
          title="状态读取失败，暂不可保存"
        />
        <el-input
          v-model="formNotes"
          type="textarea"
          :rows="3"
          :disabled="!!degraded"
          placeholder="备注：给 AI 的要求或人工点评"
          @input="formDirty = true"
        />
        <div class="save-row">
          <el-select
            v-model="formStatus"
            class="save-status"
            size="small"
            :disabled="!!degraded"
            @change="formDirty = true"
          >
            <el-option
              v-for="status in EDITABLE_STATUSES"
              :key="status"
              :label="CODE_AUDIT_STATUS_LABELS[status]"
              :value="status"
            />
          </el-select>
          <el-button
            type="primary"
            size="small"
            :loading="saving"
            :disabled="!formDirty || !!degraded"
            @click="save"
          >
            保存
          </el-button>
          <span v-if="!formDirty && !degraded" class="muted hint">与服务端一致，无改动可保存。</span>
        </div>
        <el-alert
          v-if="saveError"
          class="notice"
          type="error"
          show-icon
          :closable="false"
          :title="saveError"
          description="表单内容未被清空，修改后可再次保存。"
        />
        <el-alert
          v-if="saveOk"
          class="notice"
          type="success"
          show-icon
          :closable="false"
          :title="saveOk"
        />
      </section>

      <section class="card block">
        <p class="eyebrow">分析轮次（{{ rounds.length }}）</p>
        <p v-if="!rounds.length" class="muted none">还没有分析记录。</p>
        <el-timeline v-else>
          <el-timeline-item
            v-for="round in rounds"
            :key="round.round"
            :timestamp="round.created_at || DASH"
            placement="top"
            :type="round.proposal ? 'primary' : 'info'"
          >
            <p class="round-head">第 {{ round.round }} 轮</p>
            <p class="round-label muted">要求</p>
            <pre v-if="round.notes" class="mono pre small">{{ round.notes }}</pre>
            <p v-else class="muted none">（未记录要求）</p>
            <p class="round-label muted">方案</p>
            <pre v-if="round.proposal" class="mono pre small">{{ round.proposal }}</pre>
            <!-- An empty proposal means the model never answered, or the round
                 was recorded before the call finished. -->
            <p v-else class="muted none">（无结果）</p>
          </el-timeline-item>
        </el-timeline>
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

.fact-chip {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 0 6px;
  font-size: 11px;
  color: var(--muted);
}

.fact-file,
.fact-key {
  font-size: 11px;
  color: var(--muted);
}

.block {
  padding: 16px 18px;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.split {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
}

.split .block {
  min-width: 0;
}

.prose {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Model output and notes are unbounded; a scrolling block keeps one long
   proposal from pushing the whole page below the fold. */
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
  max-height: 320px;
  overflow: auto;
}

.pre.small {
  max-height: 220px;
}

.none {
  margin: 0;
  font-size: 12px;
}

.analyze-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 10px 0 0;
  flex-wrap: wrap;
}

.hint {
  font-size: 11px;
}

.analyze-progress {
  margin-top: 12px;
}

.reply {
  margin-top: 12px;
}

.save-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0 0;
}

.save-status {
  width: 160px;
}

.round-head {
  margin: 0 0 6px;
  font-size: 13px;
}

.round-label {
  margin: 8px 0 4px;
  font-size: 10px;
  letter-spacing: 0.6px;
}
</style>
