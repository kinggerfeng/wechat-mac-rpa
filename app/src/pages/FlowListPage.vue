<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { CopyDocument, Delete, Plus, Refresh, VideoPlay } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import { useFlowStore } from "../stores/flow";
import type { FlowGraph, FlowSummary } from "../types";

const DASH = "—";

type TagType = "success" | "info" | "warning" | "danger";

const STATUS: Record<string, { label: string; type: TagType }> = {
  ok: { label: "成功", type: "success" },
  running: { label: "运行中", type: "warning" },
  error: { label: "失败", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

type Template = "blank" | "starter";

const engine = useEngineStore();
const flow = useFlowStore();
const router = useRouter();

const loading = ref(false);
const loadError = ref("");
const rowError = ref("");
const busy = ref("");

const createOpen = ref(false);
const createName = ref("");
const createTemplate = ref<Template>("starter");
const creating = ref(false);
const createError = ref("");

const flows = computed<FlowSummary[]>(() => flow.flows);
const runningCount = computed(() => flows.value.filter((item) => item.running === true).length);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/**
 * The backend writes local-time stamps without a zone ("2026-10-02 11:53:40").
 * `new Date()` on that string is implementation-defined — WKWebView rejects it —
 * so the fields are read out and re-read as local time.
 */
function toEpoch(stamp: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(stamp.trim());
  if (!match) return null;
  return (
    new Date(
      Number(match[1]),
      Number(match[2]) - 1,
      Number(match[3]),
      Number(match[4]),
      Number(match[5]),
      Number(match[6]),
    ).getTime() / 1000
  );
}

function timeAgo(stamp: string): string {
  const epoch = toEpoch(stamp);
  if (epoch === null) return DASH;
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

function statusOf(status: string | null): { label: string; type: TagType } {
  if (!status) return { label: DASH, type: "info" };
  return STATUS[status] ?? { label: status, type: "info" };
}

function describe(item: FlowSummary): string {
  return item.description || "未填写描述";
}

function blankGraph(): FlowGraph {
  return { version: 1, entry: "", default_path: "element", variables: {}, nodes: [], edges: [] };
}

/** start → end, wired, so the new flow can be dry-run before anything else exists. */
function starterGraph(): FlowGraph {
  const base = {
    retry: { max: 0, delay: 0 },
    timeout: null,
    on_error: "fail" as const,
    disabled: false,
    path: null,
    target: null,
  };
  return {
    version: 1,
    entry: "n_start",
    default_path: "element",
    description: "最小骨架：开始 → 结束",
    variables: {},
    nodes: [
      { ...base, id: "n_start", type: "start", name: "开始", position: { x: 60, y: 200 }, params: {}, outputs: [] },
      {
        ...base,
        id: "n_end",
        type: "end",
        name: "结束",
        position: { x: 300, y: 200 },
        params: { status: "ok" },
        outputs: [],
      },
    ],
    edges: [
      {
        id: "e_start_end",
        source: "n_start",
        source_port: "ok",
        target: "n_end",
        condition: null,
        label: "",
      },
    ],
  };
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    await flow.loadFlows();
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

function openCreate(): void {
  createName.value = "";
  createTemplate.value = "starter";
  createError.value = "";
  createOpen.value = true;
}

async function create(): Promise<void> {
  const name = createName.value.trim();
  if (!name) {
    createError.value = "名称不能为空";
    return;
  }
  creating.value = true;
  createError.value = "";
  try {
    const created = await api.createFlow({
      name,
      graph: createTemplate.value === "blank" ? blankGraph() : starterGraph(),
    });
    createOpen.value = false;
    await flow.loadFlows();
    await router.push(`/canvas/${created.id}`);
  } catch (error) {
    createError.value = reason(error);
  } finally {
    creating.value = false;
  }
}

async function duplicate(item: FlowSummary): Promise<void> {
  busy.value = `${item.id}:dup`;
  rowError.value = "";
  try {
    await api.duplicateFlow(item.id);
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

/** Dry run: walks the graph and performs no real action on the desktop. */
async function runDry(item: FlowSummary): Promise<void> {
  busy.value = `${item.id}:run`;
  rowError.value = "";
  try {
    const result = await api.run(item.id, { dry_run: true });
    if (!result.accepted) rowError.value = `${item.name}：未接受运行请求`;
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

async function remove(item: FlowSummary): Promise<void> {
  busy.value = `${item.id}:del`;
  rowError.value = "";
  try {
    await api.deleteFlow(item.id);
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">FLOWS · 流程</p>
        <h1>流程</h1>
        <p class="head-note muted">
          {{ flows.length }} 条 · 校验问题在设计器内查看（列表接口不返回图，展开校验会逐个拉取整图）
        </p>
      </div>
      <div class="head-actions">
        <span v-if="runningCount" class="live"><span class="dot" />{{ runningCount }} 条执行中</span>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新建流程</el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：流程列表不可用。启动 Python 后端后重试。"
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

    <section v-if="!loading && !flows.length" class="card empty-card">
      <el-empty description="没有流程">
        <p class="empty-body muted">
          后端首次启动会写入两条种子流程（微信自动回复主循环、双路径定位演示）。
          若列表为空，说明数据目录被清空或后端尚未完成种子写入：先确认本地服务已连接，再点「刷新」。
        </p>
        <el-button size="small" type="primary" :icon="Plus" @click="openCreate">新建流程</el-button>
      </el-empty>
    </section>

    <section v-else class="card table-card">
      <el-table v-loading="loading" :data="flows" row-key="id" size="small" class="flows">
        <el-table-column label="名称" min-width="230">
          <template #default="scope">
            <div class="cell-name">
              <span class="name">
                {{ scope.row.name }}
                <span v-if="scope.row.running" class="running-flag"><span class="dot" />运行中</span>
              </span>
              <span class="desc">{{ describe(scope.row) }}</span>
              <span class="fid mono">{{ scope.row.id }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="运行次数" width="86" align="right">
          <template #default="scope">
            <span class="mono">{{ scope.row.run_count }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上次结果" width="94">
          <template #default="scope">
            <el-tag size="small" :type="statusOf(scope.row.last_status).type" disable-transitions>
              {{ statusOf(scope.row.last_status).label }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="最近更新" width="104" align="right">
          <template #default="scope">
            <span class="mono when">{{ timeAgo(scope.row.updated_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="212" align="right">
          <template #default="scope">
            <el-button size="small" link type="primary" @click="router.push(`/canvas/${scope.row.id}`)">
              设计
            </el-button>
            <el-button
              size="small"
              link
              :icon="CopyDocument"
              :loading="busy === `${scope.row.id}:dup`"
              @click="duplicate(scope.row)"
            >
              复制
            </el-button>
            <el-button
              size="small"
              link
              :icon="VideoPlay"
              :loading="busy === `${scope.row.id}:run`"
              @click="runDry(scope.row)"
            >
              运行
            </el-button>
            <el-popconfirm
              :title="`删除流程「${scope.row.name}」？该操作不可撤销。`"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              :width="240"
              @confirm="remove(scope.row)"
            >
              <template #reference>
                <el-button
                  size="small"
                  link
                  type="danger"
                  :icon="Delete"
                  :loading="busy === `${scope.row.id}:del`"
                >
                  删除
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="createOpen" title="新建流程" width="460px" append-to-body>
      <p class="dialog-note muted">两种起点都带上 default_path = 元素库，之后可在节点上单独改。</p>
      <el-form label-width="72px" size="small" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="createName" placeholder="例如：每日群发提醒" maxlength="60" />
        </el-form-item>
        <el-form-item label="模板">
          <el-radio-group v-model="createTemplate">
            <el-radio value="starter">
              最小骨架
              <span class="opt-note">已连好 开始 → 结束，可立即 dry-run 冒烟</span>
            </el-radio>
            <el-radio value="blank">
              空白图
              <span class="opt-note">没有任何节点，全部从设计器左侧拖入</span>
            </el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <p v-if="createError" class="dialog-error mono">{{ createError }}</p>
      <template #footer>
        <el-button size="small" @click="createOpen = false">取消</el-button>
        <el-button size="small" type="primary" :loading="creating" @click="create">创建并进入设计器</el-button>
      </template>
    </el-dialog>
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
  max-width: 60ch;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.live {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--path-auto);
  font-size: 10px;
}

.live .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--path-auto);
}

.notice {
  width: auto;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.flows :deep(.el-table__cell) {
  padding: 9px 0;
}

.cell-name {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.running-flag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 6px;
  border: 1px solid var(--path-auto);
  border-radius: var(--radius);
  background: var(--path-auto-soft);
  color: var(--path-auto);
  font-size: 10px;
  line-height: 1.4;
}

.running-flag .dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--path-auto);
}

.desc {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fid {
  font-size: 10px;
  color: var(--muted);
}

.when {
  color: var(--muted);
  font-size: 11px;
}

.empty-card {
  padding: 26px 18px;
}

.empty-body {
  margin: 0 auto 14px;
  max-width: 62ch;
  font-size: 11.5px;
  line-height: 1.7;
}

.dialog-note {
  margin: 0 0 14px;
  font-size: 11px;
  line-height: 1.6;
}

.opt-note {
  margin-left: 8px;
  color: var(--muted);
  font-size: 11px;
}

.dialog-error {
  margin: 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}
</style>
