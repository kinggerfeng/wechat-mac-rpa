<script setup lang="ts">
// Hosts two pre-generated HTML benchmark reports. The generator emits one
// self-contained document per report — its own <html>, <head> and <style> — so
// this page never renders report content itself. It only decides *where* a
// foreign document is mounted.

import { computed, onMounted, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { BenchmarkReport } from "@shared/types";

const DASH = "—";

type ReportKey = BenchmarkReport["key"];
type ViewMode = "embed" | "frame" | "source";

// Fixed order rather than "whatever came back": the server omits a report whose
// file is missing, and a grid that reflows from two columns to one would hide
// that absence instead of showing it.
const REPORT_KEYS: readonly ReportKey[] = ["judge", "reply"];
const FALLBACK_TITLE: Record<ReportKey, string> = {
  judge: "Judge 质量",
  reply: "Bot 回复质量",
};

interface ReportSlot {
  key: ReportKey;
  title: string;
  modified: string;
  present: boolean;
}

const reports = ref<BenchmarkReport[]>([]);
const refreshable = ref(false);
const loading = ref(false);
const loadError = ref("");
const selectedKey = ref<ReportKey | null>(null);
const view = ref<ViewMode>("frame");
const refreshing = ref(false);
const refreshError = ref("");

const hasReports = computed(() => reports.value.length > 0);
const selected = computed<BenchmarkReport | null>(
  () => reports.value.find((report) => report.key === selectedKey.value) ?? null,
);
const slots = computed<ReportSlot[]>(() =>
  REPORT_KEYS.map((key) => {
    const report = reports.value.find((item) => item.key === key);
    return {
      key,
      title: report?.title?.trim() || FALLBACK_TITLE[key],
      modified: report ? formatStamp(report.modified) : DASH,
      present: !!report,
    };
  }),
);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/**
 * `modified` is a file mtime. The server sends POSIX seconds; a value past 1e11
 * is milliseconds, and feeding those to `new Date` *as seconds* yields a
 * year-51000 stamp that looks plausible in a column and is wrong.
 */
function formatStamp(ts: number | null): string {
  if (ts === null || !Number.isFinite(ts) || ts <= 0) return DASH;
  const date = new Date((ts > 1e11 ? ts / 1000 : ts) * 1000);
  if (Number.isNaN(date.getTime())) return DASH;
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Keep the selection pointing at a report that still exists after a refresh. */
function syncSelection(): void {
  const current = selectedKey.value;
  if (current && reports.value.some((report) => report.key === current)) return;
  selectedKey.value = reports.value[0]?.key ?? null;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const bundle = await api.benchmarks();
    reports.value = bundle.reports;
    refreshable.value = bundle.refreshable;
    loadError.value = "";
    syncSelection();
  } catch (error) {
    // An API failure must not fall through to the empty state: whatever is
    // already on screen stays, and the alert carries the reason.
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

async function refresh(): Promise<void> {
  refreshing.value = true;
  refreshError.value = "";
  try {
    // The endpoint answers 200 with success:false instead of raising, so the
    // flag is the only thing that says whether the script actually ran.
    const result = await api.refreshBenchmarks();
    if (!result.success) {
      refreshError.value = result.error || "生成报告失败";
      return;
    }
    await load();
  } catch (error) {
    refreshError.value = reason(error);
  } finally {
    refreshing.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">BENCHMARKS · 质量报告</p>
        <h1>Benchmark 报告</h1>
        <p class="head-note muted">
          报告由脚本预先生成，本页只做托管与展示 · 共 {{ reports.length }} 份
        </p>
      </div>
      <div class="head-actions">
        <el-button
          v-if="refreshable"
          size="small"
          :icon="Refresh"
          :loading="refreshing"
          :disabled="loading"
          @click="refresh"
        >
          重新生成
        </el-button>
        <el-button v-else size="small" :icon="Refresh" :loading="loading" @click="load">
          刷新
        </el-button>
      </div>
    </header>

    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="loadError" />
    <el-alert
      v-if="refreshError"
      type="error"
      show-icon
      :closable="false"
      :title="refreshError"
      description="报告未更新，页面显示的仍是上一次生成的内容。"
    />
    <el-alert
      v-if="!loading && !refreshable"
      type="info"
      show-icon
      :closable="false"
      title="当前环境不支持重新生成报告"
      description="服务端没有找到生成脚本（如 tools/bench/generate_benchmark_dashboard.py），只能读取已落盘的文件。"
    />

    <el-skeleton v-if="loading && !hasReports" class="card skeleton" :rows="4" animated />

    <section v-else-if="!hasReports" class="card">
      <el-empty v-if="loadError" :description="loadError">
        <p class="empty-body muted">报告列表未能读取，因此这里没有任何内容可以展示。</p>
        <el-button size="small" :icon="Refresh" :loading="loading" @click="load">重试</el-button>
      </el-empty>
      <el-empty v-else description="尚未生成任何报告">
        <p class="empty-body muted">
          报告不是实时算的：需要先在仓库根目录执行
          <code class="mono">python3 tools/bench/generate_benchmark_dashboard.py</code>
          落盘，再回到本页刷新。
        </p>
        <el-button v-if="refreshable" type="primary" :loading="refreshing" @click="refresh">
          生成报告
        </el-button>
      </el-empty>
    </section>

    <template v-else>
      <p class="hint muted">「重新生成」会在服务端执行一次生成脚本，可能需要几十秒；完成前页面内容不会变化。</p>

      <section class="summary">
        <button
          v-for="slot in slots.filter((item) => item.present)"
          :key="slot.key"
          type="button"
          class="report-card card"
          :class="{ picked: selectedKey === slot.key }"
          @click="selectedKey = slot.key"
        >
          <span class="report-key mono">{{ slot.key }}</span>
          <span class="report-title">{{ slot.title }}</span>
          <span class="report-meta">更新于 {{ slot.modified }}</span>
        </button>
        <div
          v-for="slot in slots.filter((item) => !item.present)"
          :key="`missing-${slot.key}`"
          class="report-card card missing"
        >
          <span class="report-key mono">{{ slot.key }}</span>
          <span class="report-title">{{ slot.title }}</span>
          <span class="report-meta">文件缺失，尚未生成</span>
        </div>
      </section>

      <section v-if="selected" class="card viewer">
        <div class="viewer-head">
          <div>
            <h2>{{ selected.title }}</h2>
            <p class="muted viewer-meta mono">{{ selected.key }}</p>
          </div>
          <el-radio-group v-model="view" size="small">
            <el-radio-button value="embed">内嵌</el-radio-button>
            <el-radio-button value="frame">整页预览</el-radio-button>
            <el-radio-button value="source">源码</el-radio-button>
          </el-radio-group>
        </div>

        <!-- The report is trusted local output, but the iframe is what stops it
             restyling the console: a bare v-html lets the report's own <style>
             reach the app shell, where its `body { … }` rule would repaint
             every page in the app. `allow-scripts` is kept because the
             generator's filter tabs are inline JS, and `allow-same-origin` is
             deliberately left off so the document stays in an opaque origin. -->
        <iframe
          v-if="view === 'frame'"
          class="report-frame"
          :srcdoc="selected.html"
          sandbox="allow-scripts"
          :title="`${selected.title} 报告预览`"
        />

        <template v-else-if="view === 'embed'">
          <p class="muted embed-note">
            内嵌模式会把报告自身的 <code class="mono">&lt;style&gt;</code> 注入当前页面，可能影响控制台样式。
          </p>
          <div class="report-embed" v-html="selected.html" />
        </template>

        <pre v-else class="report-source mono">{{ selected.html }}</pre>
      </section>
    </template>
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

.hint {
  margin: 0;
  font-size: 11px;
}

.skeleton {
  padding: 18px 20px;
}

.empty-body {
  margin: 0 auto 14px;
  max-width: 520px;
  font-size: 12px;
  line-height: 1.7;
}

.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}

.report-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
  text-align: left;
  font: inherit;
  color: inherit;
  border-radius: var(--radius);
}

button.report-card {
  cursor: pointer;
}

button.report-card:hover {
  border-color: var(--green);
}

.report-card.picked {
  border-color: var(--green);
  box-shadow: inset 3px 0 0 var(--green);
}

.report-card.missing {
  border-style: dashed;
  opacity: 0.6;
}

.report-key {
  font-size: 10px;
  color: var(--muted);
}

.report-title {
  font-size: 15px;
}

.report-meta {
  font-size: 11px;
  color: var(--muted);
}

.viewer {
  padding: 16px 18px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.viewer-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.viewer-head h2 {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 19px;
  font-weight: 400;
}

.viewer-meta {
  margin: 4px 0 0;
  font-size: 10px;
}

.report-frame {
  width: 100%;
  height: 70vh;
  border: 1px solid var(--line);
  border-radius: var(--radius);
}

.report-embed {
  max-height: 70vh;
  overflow: auto;
  padding: 4px 2px;
}

.embed-note {
  margin: 0;
  font-size: 11px;
}

.report-source {
  margin: 0;
  max-height: 70vh;
  overflow: auto;
  padding: 12px;
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
