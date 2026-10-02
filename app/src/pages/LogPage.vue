<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { DocumentCopy, Download, Refresh } from "@element-plus/icons-vue";

import { useEngineStore } from "../stores/engine";

const engine = useEngineStore();

const filter = ref("");
const copyState = ref("复制");
const surface = ref<HTMLElement | null>(null);

let copyTimer: number | null = null;

const filtered = computed(() => {
  const needle = filter.value.trim().toLowerCase();
  if (!needle) return engine.logs;
  return engine.logs.filter((line) => line.toLowerCase().includes(needle));
});

const filteredText = computed(() => filtered.value.join("\n"));

function stamp(): string {
  return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
}

function scrollToEnd(): void {
  const element = surface.value;
  if (element) element.scrollTop = element.scrollHeight;
}

async function copy(): Promise<void> {
  if (!filteredText.value) return;
  try {
    await navigator.clipboard.writeText(filteredText.value);
    copyState.value = "已复制";
  } catch {
    copyState.value = "复制失败";
  }
  if (copyTimer !== null) window.clearTimeout(copyTimer);
  copyTimer = window.setTimeout(() => {
    copyState.value = "复制";
    copyTimer = null;
  }, 1600);
}

function exportLog(): void {
  if (!filteredText.value) return;
  const blob = new Blob([filteredText.value], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `wechat-rpa-${stamp()}.log`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

onMounted(scrollToEnd);
onUnmounted(() => {
  if (copyTimer !== null) window.clearTimeout(copyTimer);
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">LOGS · 日志</p>
        <h1>运行日志</h1>
        <p class="head-note muted">
          显示 {{ filtered.length }} / {{ engine.logs.length }} 行 ·
          更新于 {{ engine.lastUpdated || "—" }}
        </p>
      </div>
      <div class="head-actions">
        <el-input
          v-model="filter"
          class="filter"
          placeholder="按行过滤（不区分大小写）"
          clearable
        />
        <el-button :icon="DocumentCopy" size="small" :disabled="!filtered.length" @click="copy">
          {{ copyState }}
        </el-button>
        <el-button :icon="Download" size="small" :disabled="!filtered.length" @click="exportLog">
          导出
        </el-button>
        <el-button :icon="Refresh" size="small" :loading="engine.busy" @click="engine.refresh()">
          刷新
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      :title="engine.lastError || '本地服务离线，日志不可用。'"
    />

    <section class="card surface">
      <p v-if="!filtered.length" class="empty muted">
        {{ engine.logs.length ? "没有匹配的行。" : "暂无日志。后端启动后每 3 秒自动刷新。" }}
      </p>
      <pre v-else ref="surface" class="log mono">{{ filteredText }}</pre>
    </section>

    <button v-if="filtered.length" class="tail" type="button" @click="scrollToEnd">跳到末尾</button>
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
  gap: 8px;
  flex-wrap: wrap;
}

.filter {
  width: 240px;
}

.notice {
  width: auto;
}

.surface {
  padding: 0;
  overflow: hidden;
}

.log {
  margin: 0;
  padding: 14px 16px;
  max-height: calc(100vh - 260px);
  min-height: 240px;
  overflow: auto;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 2;
}

.empty {
  margin: 0;
  padding: 20px 16px;
  font-size: 12px;
}

.tail {
  align-self: flex-end;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--muted);
  padding: 4px 10px;
  font-size: 10px;
  cursor: pointer;
}

.tail:hover {
  color: var(--green);
  border-color: var(--green);
}
</style>
