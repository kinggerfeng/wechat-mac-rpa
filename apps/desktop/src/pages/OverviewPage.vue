<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ArrowRight, Refresh, Setting } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import type { Permission, PermissionStatus, Run, RunStatus } from "../types";

// A null reading and a real zero mean different things: "—" says the backend
// never answered, "0" says it answered and there was nothing. Collapsing the
// two would make a dead service look like a quiet day.
const DASH = "—";

const RUN_STATUS: Record<RunStatus, { label: string; type: "success" | "info" | "warning" | "danger" }> = {
  ok: { label: "成功", type: "success" },
  running: { label: "运行中", type: "warning" },
  error: { label: "失败", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

const PERMISSION_STATUS: Record<PermissionStatus, { label: string; type: "success" | "info" | "warning" | "danger" }> = {
  granted: { label: "已授权", type: "success" },
  denied: { label: "已拒绝", type: "danger" },
  not_determined: { label: "未询问", type: "warning" },
  unavailable: { label: "不可用", type: "info" },
};

const engine = useEngineStore();
const router = useRouter();

const runs = ref<Run[]>([]);
const runsLoading = ref(false);
const runsError = ref("");
const permBusy = ref("");
const permError = ref("");
const deepBusy = ref(false);

const summary = computed(() => engine.summary);

const tiles = computed(() => {
  const s = summary.value;
  return [
    { key: "ticks", label: "今日 Tick", value: s ? String(s.ticks) : DASH, hint: s ? s.date : "指标未加载" },
    { key: "replies", label: "回复数量", value: s ? String(s.replies) : DASH, hint: s ? `跳过 ${s.skipped} 次` : "指标未加载" },
    { key: "score", label: "平均 Judge 分", value: s ? s.avg_score.toFixed(1) : DASH, hint: s ? "满分 100" : "指标未加载" },
    { key: "skip", label: "跳过率", value: s ? `${s.skip_rate}%` : DASH, hint: s ? "占比今日 Tick" : "指标未加载" },
  ];
});

const botRows = computed(() => {
  const s = engine.status;
  return [
    { label: "Bot 进程", value: s === null ? DASH : s.running ? "运行中" : "已停止" },
    { label: "PID", value: s?.pid ? String(s.pid) : DASH },
    { label: "模型", value: s?.model || DASH },
    { label: "微信连接", value: engine.wechatLabel, tone: engine.wechatReady ? "green" : "warn" },
  ];
});

const permissions = computed<Permission[]>(() => engine.permissions?.permissions ?? []);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function timeAgo(ts: number | null): string {
  if (!ts) return DASH;
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

async function loadRuns(): Promise<void> {
  runsLoading.value = true;
  try {
    runs.value = (await api.runs({ limit: 5 })).runs;
    runsError.value = "";
  } catch (error) {
    runsError.value = reason(error);
  } finally {
    runsLoading.value = false;
  }
}

async function openPane(perm: Permission): Promise<void> {
  permBusy.value = `${perm.key}:pane`;
  permError.value = "";
  try {
    await api.openPermissionPane(perm.key);
  } catch (error) {
    permError.value = reason(error);
  } finally {
    permBusy.value = "";
  }
}

async function prompt(perm: Permission): Promise<void> {
  permBusy.value = `${perm.key}:prompt`;
  permError.value = "";
  try {
    const result = await api.promptPermission(perm.key);
    await engine.refreshPermissions(false);
    if (!result.prompted) permError.value = `${perm.title}：系统未弹出授权请求`;
    else if (!result.granted) permError.value = `${perm.title} 未授权 · ${perm.fix}`;
  } catch (error) {
    permError.value = reason(error);
  } finally {
    permBusy.value = "";
  }
}

// The deep check reads the TCC database, which macOS treats as a consent
// prompt. It therefore has exactly one entry point: this button.
async function deepCheck(): Promise<void> {
  deepBusy.value = true;
  permError.value = "";
  try {
    await engine.refreshPermissions(true);
  } finally {
    deepBusy.value = false;
  }
}

onMounted(() => {
  void loadRuns();
  void engine.refreshPermissions(false);
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">OVERVIEW · 运行总览</p>
        <h1>今天</h1>
      </div>
      <el-button :icon="Refresh" :loading="runsLoading" size="small" @click="loadRuns">
        刷新
      </el-button>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：指标与运行记录不可用。启动 Python 后端后重试。"
    />

    <section class="tiles">
      <article v-for="tile in tiles" :key="tile.key" class="card tile">
        <p class="tile-label">{{ tile.label }}</p>
        <p class="tile-value">{{ tile.value }}</p>
        <p class="tile-hint mono">{{ tile.hint }}</p>
      </article>
    </section>

    <section class="card band">
      <p class="eyebrow">STATUS · 状态</p>
      <dl class="band-grid">
        <div v-for="row in botRows" :key="row.label" class="band-cell">
          <dt>{{ row.label }}</dt>
          <dd :class="{ green: row.tone === 'green', warn: row.tone === 'warn' }">{{ row.value }}</dd>
        </div>
      </dl>
      <p class="band-foot muted">
        数据更新于 {{ engine.lastUpdated || DASH }} ·
        启停 Bot 在顶栏操作
      </p>
    </section>

    <section class="card perms">
      <div class="perms-head">
        <p class="eyebrow">PERMISSIONS · 权限</p>
        <el-button :icon="Setting" size="small" :loading="deepBusy" @click="deepCheck">
          深度检测
        </el-button>
      </div>
      <p v-if="!permissions.length" class="muted empty">权限报告未加载。</p>
      <ul v-else class="perm-list">
        <li v-for="perm in permissions" :key="perm.key" class="perm-row">
          <div class="perm-text">
            <span class="perm-title">{{ perm.title }}</span>
            <el-tag size="small" :type="PERMISSION_STATUS[perm.status].type" disable-transitions>
              {{ PERMISSION_STATUS[perm.status].label }}
            </el-tag>
            <span class="perm-detail muted">{{ perm.detail }}</span>
            <span v-if="perm.gates" class="perm-gates mono">门控：{{ perm.gates }}</span>
          </div>
          <div class="perm-actions">
            <el-button
              size="small"
              :icon="Setting"
              :loading="permBusy === `${perm.key}:pane`"
              @click="openPane(perm)"
            >
              打开设置面板
            </el-button>
            <el-button
              size="small"
              :loading="permBusy === `${perm.key}:prompt`"
              :disabled="perm.status === 'granted'"
              @click="prompt(perm)"
            >
              重新检测
            </el-button>
          </div>
        </li>
      </ul>
      <p v-if="permError" class="perm-error mono">{{ permError }}</p>
    </section>

    <section class="card recent">
      <div class="perms-head">
        <p class="eyebrow">RECENT · 最近运行</p>
        <el-button size="small" :icon="ArrowRight" @click="router.push('/runs')">
          全部记录
        </el-button>
      </div>
      <p v-if="runsError" class="perm-error mono">{{ runsError }}</p>
      <p v-else-if="!runs.length" class="muted empty">暂无运行记录。</p>
      <ul v-else class="run-list">
        <li v-for="run in runs" :key="run.id" class="run-row">
          <span class="run-name">{{ run.flow_name || run.flow_id }}</span>
          <el-tag size="small" :type="RUN_STATUS[run.status].type" disable-transitions>
            {{ RUN_STATUS[run.status].label }}
          </el-tag>
          <span class="run-meta mono">{{ timeAgo(run.started_at) }} · {{ run.steps }} 步</span>
          <el-button size="small" link type="primary" :icon="ArrowRight" @click="router.push('/runs')">
            查看
          </el-button>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: 24px 28px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
}

.page-head h1 {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 400;
  letter-spacing: 0.5px;
}

.notice {
  width: auto;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.tile {
  padding: 16px 18px;
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
  font-size: 32px;
  line-height: 1;
  color: var(--ink);
}

.tile-hint {
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.band {
  padding: 16px 18px;
}

.band-grid {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.band-cell dt {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.6px;
}

.band-cell dd {
  margin: 6px 0 0;
  font-size: 14px;
  color: var(--ink);
}

.band-cell dd.green {
  color: var(--green);
}

.band-cell dd.warn {
  color: var(--orange);
}

.band-foot {
  margin: 16px 0 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  font-size: 10px;
}

.perms,
.recent {
  padding: 16px 18px;
}

.perms-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.perms-head .eyebrow {
  margin: 0;
}

.perm-list,
.run-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.perm-row,
.run-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--line);
}

.perm-row:first-child,
.run-row:first-child {
  border-top: 0;
}

.perm-text {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.perm-title {
  font-size: 13px;
}

.perm-detail {
  font-size: 11px;
}

.perm-gates {
  font-size: 10px;
  color: var(--muted);
}

.perm-actions {
  display: flex;
  gap: 8px;
  flex: 0 0 auto;
}

.perm-error {
  margin: 12px 0 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}

.run-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-meta {
  color: var(--muted);
  font-size: 10px;
  flex: 0 0 auto;
}

.empty {
  margin: 0;
  padding: 14px 0;
  font-size: 12px;
}

@media (max-width: 960px) {
  .tiles,
  .band-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
