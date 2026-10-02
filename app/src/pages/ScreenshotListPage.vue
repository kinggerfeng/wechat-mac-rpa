<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import type { ScreenshotRow } from "../types";

const DASH = "—";

const PAGE_SIZES = [24, 48, 96];
const SKELETON_TILES = 12;

const engine = useEngineStore();

const rows = ref<ScreenshotRow[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(24);
const loading = ref(false);
const loadError = ref("");

/** A failure must not read as "there are no screenshots". */
const isEmpty = computed(() => !loading.value && !loadError.value && rows.value.length === 0);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/**
 * The image endpoint accepts a bare filename and rejects anything containing a
 * path separator, so the full `screenshot_path` 404s. This is the one helper
 * that stands between the DB column and the request.
 */
function basename(path: string | null): string {
  if (!path) return "";
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

/** null → render the <img>; otherwise the placeholder text saying why not. */
function placeholder(row: ScreenshotRow): string | null {
  if (!row.has_image) return "无截图";
  if (!basename(row.screenshot_path)) return "截图路径为空";
  return null;
}

function imageUrl(row: ScreenshotRow): string {
  return api.screenshotImageUrl(basename(row.screenshot_path));
}

/** `created_at` is a local-time string from SQLite, not an epoch. The stamp is
 *  shown verbatim — reformatting it here is one more place to render a wrong
 *  date, and the raw value is already short enough for a tile. */
function shortStamp(stamp: string | null): string {
  if (!stamp) return DASH;
  return stamp.length > 16 ? stamp.slice(0, 16) : stamp;
}

function onPageChange(next: number): void {
  if (!Number.isFinite(next) || next === page.value) return;
  page.value = next;
  void load();
}

/** A different page size invalidates the current offset, so go back to page 1. */
function onSizeChange(next: number): void {
  if (!Number.isFinite(next) || next <= 0 || next === pageSize.value) return;
  pageSize.value = next;
  page.value = 1;
  void load();
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await api.screenshots({ page: page.value, size: pageSize.value });
    rows.value = result.rows;
    total.value = result.total;
  } catch (error) {
    // Keep the previous tiles: blanking the grid on a transient failure is
    // indistinguishable from "the bot has never taken a screenshot".
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
        <p class="eyebrow">SCREENSHOTS · 截图</p>
        <h1>截图</h1>
        <p class="head-note muted">
          每个 tick 在微信窗口上的截图，按时间倒序。点任意一张进入详情，那里能看到这一轮的
          元数据、Judge 分和实际发出的回复。数据更新于 {{ engine.lastUpdated || DASH }}。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="!engine.online"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：无法读取截图列表。启动 Python 后端后重试。"
    />
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="`截图列表加载失败：${loadError}`"
    />

    <div v-if="loading" class="grid">
      <div v-for="n in SKELETON_TILES" :key="n" class="card tile is-skeleton">
        <el-skeleton animated :rows="2" />
      </div>
    </div>

    <section v-else-if="isEmpty" class="card empty-card">
      <el-empty
        description="没有截图：这一轮还没有 tick 记录，或每条 tick 都没有落盘图片。"
        :image-size="72"
      />
    </section>

    <div v-else class="grid">
      <router-link
        v-for="row in rows"
        :key="row.tick_id"
        class="card tile"
        :to="{ name: 'screenshot-detail', params: { tickId: row.tick_id } }"
      >
        <div class="shot">
          <el-image
            v-if="!placeholder(row)"
            class="thumb"
            :src="imageUrl(row)"
            fit="cover"
            lazy
            :alt="`${row.chat_name || DASH} 的窗口截图`"
          >
            <template #error>
              <div class="slot">
                <span class="slot-title">截图不可用</span>
                <span class="slot-note">图片文件读取失败</span>
              </div>
            </template>
          </el-image>
          <div v-else class="slot is-empty">
            <span class="slot-title">{{ placeholder(row) }}</span>
            <span class="slot-note">这条 tick 没有可显示的图片</span>
          </div>
        </div>
        <div class="caption">
          <span class="chat">{{ row.chat_name || DASH }}</span>
          <span class="meta mono">{{ shortStamp(row.created_at) }} · #{{ row.tick_id }}</span>
        </div>
      </router-link>
    </div>

    <footer v-if="!loading && total > 0" class="pager">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="PAGE_SIZES"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        small
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </footer>
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

.empty-card {
  padding: 10px 0;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 12px;
}

.tile {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  text-decoration: none;
  color: inherit;
  transition: border-color 0.15s ease;
}

.tile:hover {
  border-color: var(--green);
}

.tile.is-skeleton {
  padding: 10px;
  min-height: 178px;
}

.shot {
  height: 128px;
  background: var(--canvas);
  border-bottom: 1px solid var(--line);
}

.thumb {
  width: 100%;
  height: 100%;
  display: block;
}

.thumb :deep(.el-image__inner) {
  width: 100%;
  height: 100%;
}

.slot {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px;
  text-align: center;
}

.slot.is-empty {
  border: 1px dashed var(--line);
}

.slot-title {
  font-size: 11.5px;
  color: var(--orange);
}

.slot.is-empty .slot-title {
  color: var(--muted);
}

.slot-note {
  font-size: 9.5px;
  color: var(--muted);
  line-height: 1.5;
}

.caption {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px 9px;
  min-width: 0;
}

.chat {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  font-size: 9.5px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
}
</style>
