<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ArrowLeft, Refresh, TopRight } from "@element-plus/icons-vue";
import { useRoute } from "vue-router";

import { api, ApiError } from "../api/client";
import type { ScreenshotDetail } from "../types";

const DASH = "—";

const route = useRoute();

const detail = ref<ScreenshotDetail | null>(null);
const loading = ref(false);
const loadError = ref("");
const imageFailed = ref(false);

/** The URL param is a string, and the API wants a positive integer. A regex
 *  rather than `Number()` so "1.5", "12abc", "1e3" and "-3" are all rejected
 *  instead of quietly becoming some other tick. */
function parseTickId(raw: unknown): number | null {
  const text = Array.isArray(raw) ? raw[0] : raw;
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const id = Number(trimmed);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

const tickId = computed(() => parseTickId(route.params.tickId));

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

const imageName = computed(() => {
  const path = detail.value?.screenshot_path;
  if (!path) return "";
  // The image endpoint only accepts a bare filename; the directory half of
  // screenshot_path makes it 404.
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
});

const imageUrl = computed(() =>
  imageName.value ? api.screenshotImageUrl(imageName.value) : "",
);

/** "" → render the image; otherwise the reason it is not shown. */
const imageUnavailable = computed(() => {
  const row = detail.value;
  if (!row) return "";
  if (!row.has_image) return "这条 tick 没有截图文件";
  if (!imageName.value) return "截图路径为空，无法生成图片地址";
  if (imageFailed.value) return "图片加载失败：文件可能已被清理，或后端服务未运行";
  return "";
});

/** The type says `number`, but the column in tick_log is nullable — a null
 *  arriving here would render the link with an empty param. Guard anyway. */
const tickDetailId = computed<number | null>(() => {
  const id = detail.value?.tick_id;
  return typeof id === "number" && Number.isSafeInteger(id) && id > 0 ? id : null;
});

function score(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return DASH;
  return value.toFixed(1);
}

/** The SQLite local-time stamp, shown verbatim — reformatting a date is one
 *  more place to render the wrong one. */
function stamp(value: string | null): string {
  return value || DASH;
}

async function load(id: number): Promise<void> {
  loading.value = true;
  loadError.value = "";
  imageFailed.value = false;
  try {
    detail.value = await api.screenshot(id);
  } catch (error) {
    detail.value = null;
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

// Watched rather than fetched on mount: moving between two detail pages keeps
// the same component instance, and a stale body next to a new URL is the worst
// of both worlds.
watch(
  tickId,
  (id) => {
    if (id === null) {
      detail.value = null;
      loadError.value = "";
      return;
    }
    void load(id);
  },
  { immediate: true },
);

function refresh(): void {
  if (tickId.value !== null) void load(tickId.value);
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">SCREENSHOT · 截图详情</p>
        <h1>截图详情</h1>
        <p v-if="detail" class="head-note muted">
          {{ detail.chat_name || DASH }} · {{ stamp(detail.created_at) }}
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="ArrowLeft" size="small" @click="$router.push({ name: 'screenshots' })">
          返回截图列表
        </el-button>
        <el-button
          :icon="Refresh"
          size="small"
          :loading="loading"
          :disabled="tickId === null"
          @click="refresh"
        >
          刷新
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="`截图详情加载失败：${loadError}`"
    />

    <!-- Guarded before the API is touched at all: a bad URL is a broken link,
         not a missing record, and it must not read as "not found". -->
    <el-alert
      v-if="tickId === null"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="链接无效：地址里的 tick id 不是正整数。"
    />

    <el-skeleton v-else-if="loading" :rows="8" animated />

    <el-empty
      v-else-if="!loadError && !detail"
      description="找不到这条 tick 的截图记录。"
      :image-size="72"
    />

    <div v-else-if="detail" class="layout">
      <section class="card shot-card">
        <div class="shot-head">
          <h2>窗口截图</h2>
          <a
            v-if="!imageUnavailable"
            class="open-link"
            :href="imageUrl"
            target="_blank"
            rel="noopener"
          >
            在新窗口打开原图
            <el-icon class="open-icon"><TopRight /></el-icon>
          </a>
        </div>
        <div class="shot">
          <!-- The failure is tracked in state rather than filled in by the
               #error slot: the "open original" link above reads the same flag
               and must disappear too, and the panel has to name the cause. -->
          <el-image
            v-if="!imageUnavailable"
            class="detail-img"
            :src="imageUrl"
            fit="contain"
            :alt="`${detail.chat_name || DASH} 的窗口截图`"
            @error="imageFailed = true"
          />
          <div v-else class="unavailable">
            <span class="unavailable-title">截图不可用</span>
            <span class="unavailable-note">{{ imageUnavailable }}</span>
          </div>
        </div>
      </section>

      <aside class="side">
        <section class="card pad">
          <h2>元数据</h2>
          <el-descriptions :column="1" size="small" border class="meta">
            <el-descriptions-item label="tick_id">
              <span class="mono">{{ detail.tick_id ?? DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="session_id">
              <span class="mono">{{ detail.session_id || DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="聊天对象">
              {{ detail.chat_name || DASH }}
            </el-descriptions-item>
            <el-descriptions-item label="时间">
              <span class="mono">{{ stamp(detail.created_at) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="Judge 分">
              <span class="mono">{{ score(detail.judge_score) }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="detail.skip_reason" label="跳过原因">
              <span class="skip">{{ detail.skip_reason }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="截图文件">
              <span class="mono path" :title="detail.screenshot_path || ''">
                {{ imageName || DASH }}
              </span>
            </el-descriptions-item>
          </el-descriptions>
          <router-link
            v-if="tickDetailId !== null"
            class="tick-link"
            :to="{ name: 'tick-detail', params: { id: tickDetailId } }"
          >
            查看这条 tick 的完整记录
          </router-link>
        </section>

        <section class="card pad">
          <h2>本轮发出的回复</h2>
          <p v-if="detail.reply" class="reply">{{ detail.reply }}</p>
          <p v-else class="muted no-reply">{{ DASH }}（这一轮没有发出回复）</p>
        </section>
      </aside>
    </div>
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

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 14px;
  align-items: start;
}

@media (max-width: 1100px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

.shot-card {
  padding: 12px 14px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.shot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

h2 {
  margin: 0 0 10px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--muted);
}

.shot-head h2 {
  margin: 0;
}

.open-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--path-element);
  text-decoration: none;
}

.open-link:hover {
  text-decoration: underline;
}

.open-icon {
  font-size: 12px;
}

.shot {
  height: 560px;
  background: var(--canvas);
  border: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.detail-img {
  width: 100%;
  height: 100%;
}

.detail-img :deep(.el-image__inner) {
  width: 100%;
  height: 100%;
}

.unavailable {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 24px;
  text-align: center;
}

.unavailable-title {
  font-size: 13px;
  color: var(--orange);
}

.unavailable-note {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.7;
  max-width: 46ch;
}

.side {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.pad {
  padding: 12px 14px 14px;
}

.meta {
  font-size: 11.5px;
}

.meta :deep(.el-descriptions__label) {
  color: var(--muted);
  width: 88px;
}

.path {
  font-size: 10.5px;
  color: var(--muted);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skip {
  color: var(--orange);
}

.tick-link {
  display: inline-block;
  margin-top: 12px;
  font-size: 11px;
  color: var(--path-element);
  text-decoration: none;
}

.tick-link:hover {
  text-decoration: underline;
}

.reply {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}

.no-reply {
  margin: 0;
  font-size: 12px;
}
</style>
