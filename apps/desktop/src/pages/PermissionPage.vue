<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { CopyDocument, Lock, MagicStick, Refresh, Setting } from "@element-plus/icons-vue";

import { API_BASE, api, ApiError } from "@shared/api/client";
import { useEngineStore } from "../stores/engine";
import type { Permission, PermissionReport, PermissionStatus, WebhookInfo } from "@shared/types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";

const PERMISSION_STATUS: Record<PermissionStatus, { label: string; type: TagType }> = {
  granted: { label: "已授权", type: "success" },
  denied: { label: "未授权", type: "danger" },
  not_determined: { label: "未询问", type: "warning" },
  unavailable: { label: "无法检测", type: "info" },
};

const engine = useEngineStore();

const report = ref<PermissionReport | null>(null);
const loading = ref(false);
const loadError = ref("");
const rowError = ref("");
const busy = ref("");

const deepBusy = ref(false);
const deepWarning = ref("");

const visionBusy = ref(false);
const visionNote = ref("");

const webhook = ref<WebhookInfo | null>(null);
const webhookBusy = ref(false);
const webhookNote = ref("");

const permissions = computed<Permission[]>(() => report.value?.permissions ?? []);
const missing = computed<string[]>(() => report.value?.missing ?? []);
const isDeep = computed(() => report.value?.deep === true);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function statusOf(permission: Permission): { label: string; type: TagType } {
  return PERMISSION_STATUS[permission.status];
}

/** Shallow by default: `deep=true` reads the TCC database, which macOS treats
 *  as a consent prompt, so it has exactly one entry point — the button below. */
async function load(deep = false): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    report.value = await api.permissions(deep);
    if (deep) deepWarning.value = "";
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

async function deepCheck(): Promise<void> {
  deepBusy.value = true;
  deepWarning.value = "";
  try {
    await load(true);
  } finally {
    deepBusy.value = false;
  }
}

async function openPane(permission: Permission): Promise<void> {
  busy.value = `${permission.key}:pane`;
  rowError.value = "";
  try {
    const result = await api.openPermissionPane(permission.key);
    if (!result.opened) rowError.value = `${permission.title}：系统未打开设置面板`;
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

async function prompt(permission: Permission): Promise<void> {
  busy.value = `${permission.key}:prompt`;
  rowError.value = "";
  try {
    const result = await api.promptPermission(permission.key);
    await load(false);
    if (!result.prompted) rowError.value = `${permission.title}：系统未弹出授权请求，需到设置面板手动勾选`;
    else if (!result.granted) rowError.value = `${permission.title} 未授权 · ${permission.fix}`;
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

async function resetVision(): Promise<void> {
  visionBusy.value = true;
  visionNote.value = "";
  try {
    await api.resetVisionClient();
    visionNote.value = "已清除缓存的视觉客户端。下一次调用会按当前环境变量重建。";
  } catch (error) {
    visionNote.value = reason(error);
  } finally {
    visionBusy.value = false;
  }
}

async function loadWebhook(): Promise<void> {
  webhookBusy.value = true;
  webhookNote.value = "";
  try {
    webhook.value = await api.webhookInfo();
  } catch (error) {
    webhookNote.value = reason(error);
  } finally {
    webhookBusy.value = false;
  }
}

async function copyWebhook(): Promise<void> {
  const path = webhook.value?.path;
  if (!path) return;
  webhookNote.value = "";
  try {
    await navigator.clipboard.writeText(`${API_BASE}${path}`);
    webhookNote.value = "已复制到剪贴板。";
  } catch {
    webhookNote.value = "复制失败：请手动选中地址复制。";
  }
}

onMounted(() => {
  void load(false);
  void loadWebhook();
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">PERMISSIONS · 权限与设置</p>
        <h1>权限与设置</h1>
        <p class="head-note muted">
          定位分两条路径：元素库在本地解析，需要屏幕录制与辅助功能；多模态路径把截图发给模型，需要 DASHSCOPE_API_KEY。
          Webhook 是外部触发入口，未配置时不开放。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load(false)">刷新</el-button>
        <el-button type="warning" plain size="small" :icon="Lock" :loading="deepBusy" @click="deepCheck">
          深度检测
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：权限报告不可用。启动 Python 后端后重试。"
    />
    <el-alert
      v-if="loadError"
      class="notice"
      type="error"
      show-icon
      :closable="false"
      :title="loadError"
    />
    <el-alert
      v-if="rowError"
      class="notice"
      type="error"
      show-icon
      closable
      @close="rowError = ''"
      :title="rowError"
    />
    <el-alert
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="深度检测会读取 macOS 的 TCC 授权数据库，系统可能弹出同意对话框。仅在你刚改过系统设置、需要确认状态时手动触发；页面加载与轮询一律只做浅检测。"
    />

    <section class="card perms">
      <div class="band-head">
        <p class="eyebrow">REPORT · 权限报告</p>
        <span class="stamp">
          <el-tag size="small" :type="isDeep ? 'warning' : 'info'" disable-transitions>
            {{ isDeep ? "深度检测" : "浅检测" }}
          </el-tag>
          <span class="muted">{{ report?.summary || DASH }}</span>
        </span>
      </div>

      <p class="context muted">
        微信状态：{{ report?.wechat_running || DASH }} · 缺失项：
        <span class="mono">{{ missing.length ? missing.join("、") : "无" }}</span>
      </p>

      <p v-if="!permissions.length" class="muted empty">权限报告未加载。</p>
      <ul v-else class="perm-list">
        <li v-for="permission in permissions" :key="permission.key" class="perm-row">
          <div class="perm-text">
            <div class="perm-line">
              <span class="perm-title">{{ permission.title }}</span>
              <el-tag size="small" :type="statusOf(permission).type" disable-transitions>
                {{ statusOf(permission).label }}
              </el-tag>
              <span class="perm-key mono">{{ permission.key }}</span>
            </div>
            <p class="perm-detail">{{ permission.detail }}</p>
            <p v-if="permission.gates" class="perm-gates mono">门控：{{ permission.gates }}</p>
            <p v-if="!permission.granted && permission.fix" class="perm-fix mono">修复：{{ permission.fix }}</p>
          </div>
          <div class="perm-actions">
            <el-button
              size="small"
              :icon="Setting"
              :loading="busy === `${permission.key}:pane`"
              @click="openPane(permission)"
            >
              打开系统设置
            </el-button>
            <el-button
              size="small"
              :loading="busy === `${permission.key}:prompt`"
              :disabled="permission.status === 'granted'"
              @click="prompt(permission)"
            >
              请求授权
            </el-button>
          </div>
        </li>
      </ul>
      <p v-if="deepWarning" class="note mono">{{ deepWarning }}</p>
    </section>

    <section class="card band">
      <div class="band-head">
        <p class="eyebrow">VISION · 视觉模型</p>
        <el-button
          size="small"
          :icon="MagicStick"
          :loading="visionBusy"
          @click="resetVision"
        >
          重置视觉客户端缓存
        </el-button>
      </div>
      <ul class="notes">
        <li>多模态路径（节点 path = vision 或 auto 降级）必须设置环境变量 <span class="mono">DASHSCOPE_API_KEY</span>，否则整条路径不可用。</li>
        <li>进程在启动时读取该变量；改了 key 之后必须重启当前进程才会生效。重置缓存只清掉已构建的客户端，不会重新读取环境变量。</li>
        <li>调用会产生费用。节点上用 element_strict 可以保证绝不落到模型。</li>
      </ul>
      <p v-if="visionNote" class="note mono">{{ visionNote }}</p>
    </section>

    <section class="card band">
      <div class="band-head">
        <p class="eyebrow">WEBHOOK · 外部触发</p>
        <el-button size="small" :icon="Refresh" :loading="webhookBusy" @click="loadWebhook">刷新</el-button>
      </div>

      <p v-if="!webhook" class="muted empty">webhook 状态未加载。</p>
      <template v-else-if="!webhook.configured">
        <p class="hint">未配置：{{ webhook.hint }}</p>
        <p class="notes-line muted">
          在启动 Python 后端的同一个环境里设置 <span class="mono">RPA_WEBHOOK_TOKEN=任意长随机串</span>，
          然后重启后端。token 即凭据，留空则 <span class="mono">POST /api/webhook/&lt;token&gt;</span> 不存在，外部无法触发流程。
        </p>
      </template>
      <template v-else>
        <div class="hook">
          <code class="hook-path mono">{{ API_BASE }}{{ webhook.path }}</code>
          <el-button size="small" :icon="CopyDocument" @click="copyWebhook">复制</el-button>
        </div>
        <p class="hint warn">{{ webhook.hint }}</p>
        <p class="notes-line muted">
          调用方式：<span class="mono">POST {{ webhook.path }}</span>，请求体
          <span class="mono">{"flow_id": "&lt;流程 id&gt;", "variables": {}}</span>。
        </p>
      </template>
      <p v-if="webhookNote" class="note mono">{{ webhookNote }}</p>
    </section>
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

.perms,
.band {
  padding: 16px 18px;
}

.band-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.band-head .eyebrow {
  margin: 0 0 10px;
}

.stamp {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  margin-bottom: 10px;
}

.context {
  margin: 0 0 4px;
  font-size: 11px;
}

.perm-list,
.notes {
  margin: 0;
  padding: 0;
  list-style: none;
}

.perm-row {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
}

.perm-row:first-child {
  border-top: 0;
}

.perm-text {
  flex: 1;
  min-width: 0;
}

.perm-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.perm-title {
  font-size: 13px;
}

.perm-key {
  font-size: 10px;
  color: var(--muted);
}

.perm-detail {
  margin: 6px 0 0;
  font-size: 11.5px;
}

.perm-gates,
.perm-fix {
  margin: 4px 0 0;
  font-size: 10.5px;
  color: var(--muted);
}

.perm-fix {
  color: var(--orange);
}

.perm-actions {
  display: flex;
  gap: 8px;
  flex: 0 0 auto;
}

.notes {
  padding-left: 4px;
}

.notes li {
  position: relative;
  padding-left: 14px;
  margin-top: 6px;
  font-size: 11.5px;
  line-height: 1.7;
}

.notes li::before {
  content: "—";
  position: absolute;
  left: 0;
  color: var(--muted);
}

.notes-line {
  margin: 8px 0 0;
  font-size: 11.5px;
  line-height: 1.7;
}

.hint {
  margin: 4px 0 0;
  font-size: 12px;
}

.hint.warn {
  color: var(--orange);
}

.hook {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 4px 0 0;
  flex-wrap: wrap;
}

.hook-path {
  padding: 6px 9px;
  border: 1px solid var(--line);
  background: var(--canvas);
  border-radius: var(--radius);
  font-size: 11.5px;
  word-break: break-all;
}

.note {
  margin: 12px 0 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}

.empty {
  margin: 0;
  padding: 12px 0;
  font-size: 12px;
}
</style>
