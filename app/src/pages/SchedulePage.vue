<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { AlarmClock, Delete, EditPen, Plus, Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import { useFlowStore } from "../stores/flow";
import type { CronPreview, Schedule, ScheduleStatus } from "../types";

const DASH = "—";
const DEBOUNCE_MS = 400;

type TagType = "success" | "info" | "warning" | "danger";

const RUN_STATUS: Record<string, { label: string; type: TagType }> = {
  ok: { label: "成功", type: "success" },
  started: { label: "已启动", type: "success" },
  running: { label: "运行中", type: "warning" },
  error: { label: "失败", type: "danger" },
  refused: { label: "被拒", type: "danger" },
  aborted: { label: "已中止", type: "info" },
};

interface ScheduleForm {
  id: string | null;
  name: string;
  flow_id: string;
  cron: string;
  enabled: boolean;
}

const engine = useEngineStore();
const flow = useFlowStore();

const schedules = ref<Schedule[]>([]);
const schedulerRunning = ref(false);
const standby = ref(false);
const standbyReason = ref("");
const leasePid = ref<number | null>(null);
const stats = ref<Record<string, number | string>>({});
const loading = ref(false);
const loadError = ref("");
const rowError = ref("");
const busy = ref("");

const dialogOpen = ref(false);
const saving = ref(false);
const saveError = ref("");
const form = ref<ScheduleForm>({ id: null, name: "", flow_id: "", cron: "", enabled: true });

const preview = ref<CronPreview | null>(null);
const previewBusy = ref(false);

let debounceTimer: number | null = null;
/** Guards against an older preview landing after a newer one. */
let previewSeq = 0;

const editing = computed(() => form.value.id !== null);
const cronValid = computed(() => preview.value?.valid === true);
const nextRuns = computed(() => preview.value?.next_runs ?? []);
const canSave = computed(
  () => form.value.name.trim() !== "" && form.value.flow_id !== "" && cronValid.value && !saving.value,
);

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** Local-time stamp without a zone — parsed field by field on purpose. */
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

function timeAgoEpoch(epoch: number | null): string {
  if (!epoch) return DASH;
  // Seconds, not milliseconds: the scheduler stamps `time.time()`.
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

function timeAgo(stamp: string | null): string {
  if (!stamp) return DASH;
  return timeAgoEpoch(toEpoch(stamp));
}

function statNumber(key: string): string {
  const value = stats.value[key];
  return typeof value === "number" ? String(value) : DASH;
}

function statText(key: string): string {
  const value = stats.value[key];
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return DASH;
}

/** "2026-10-02T12:30" → "10-02 12:30". */
function moment(iso: string): string {
  return iso.replace("T", " ").slice(5);
}

function statusOf(status: string | null): { label: string; type: TagType } {
  if (!status) return { label: DASH, type: "info" };
  return RUN_STATUS[status] ?? { label: status, type: "info" };
}

function nextFire(row: Schedule): string {
  const first = row.cron_preview?.[0];
  return first ? moment(first) : DASH;
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    apply(await api.schedules());
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

function apply(result: ScheduleStatus): void {
  schedules.value = result.schedules;
  schedulerRunning.value = result.running;
  standby.value = result.standby;
  standbyReason.value = result.standby_reason;
  leasePid.value = result.lease?.pid ?? null;
  stats.value = result.stats;
}

async function runPreview(cron: string): Promise<void> {
  const trimmed = cron.trim();
  previewSeq += 1;
  const seq = previewSeq;
  if (!trimmed) {
    preview.value = null;
    previewBusy.value = false;
    return;
  }
  previewBusy.value = true;
  try {
    const result = await api.previewCron(trimmed);
    if (seq === previewSeq) preview.value = result;
  } catch (error) {
    if (seq === previewSeq) preview.value = { valid: false, error: reason(error) };
  } finally {
    if (seq === previewSeq) previewBusy.value = false;
  }
}

/** Preview on every keystroke, debounced: each call is a round trip to the
 *  scheduler's own cron parser. */
function onCronInput(): void {
  if (debounceTimer !== null) window.clearTimeout(debounceTimer);
  previewBusy.value = true;
  debounceTimer = window.setTimeout(() => {
    debounceTimer = null;
    void runPreview(form.value.cron);
  }, DEBOUNCE_MS);
}

function openCreate(): void {
  form.value = { id: null, name: "", flow_id: flow.flows[0]?.id ?? "", cron: "*/15 * * * *", enabled: true };
  preview.value = null;
  saveError.value = "";
  dialogOpen.value = true;
  void runPreview(form.value.cron);
}

function openEdit(row: Schedule): void {
  form.value = {
    id: row.id,
    name: row.name,
    flow_id: row.flow_id,
    cron: row.cron,
    enabled: row.enabled === 1,
  };
  preview.value = null;
  saveError.value = "";
  dialogOpen.value = true;
  void runPreview(row.cron);
}

async function save(): Promise<void> {
  if (!canSave.value) return;
  saving.value = true;
  saveError.value = "";
  try {
    await api.saveSchedule({
      id: form.value.id ?? undefined,
      name: form.value.name.trim(),
      flow_id: form.value.flow_id,
      cron: form.value.cron.trim(),
      enabled: form.value.enabled ? 1 : 0,
    });
    dialogOpen.value = false;
    await load();
  } catch (error) {
    saveError.value = reason(error);
  } finally {
    saving.value = false;
  }
}

async function toggle(row: Schedule, value: string | number | boolean): Promise<void> {
  busy.value = `${row.id}:toggle`;
  rowError.value = "";
  try {
    await api.saveSchedule({
      id: row.id,
      name: row.name,
      flow_id: row.flow_id,
      cron: row.cron,
      enabled: value ? 1 : 0,
    });
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

async function remove(row: Schedule): Promise<void> {
  busy.value = `${row.id}:del`;
  rowError.value = "";
  try {
    await api.deleteSchedule(row.id);
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

onMounted(() => {
  void load();
  void flow.loadFlows().catch(() => undefined);
});

onUnmounted(() => {
  if (debounceTimer !== null) {
    window.clearTimeout(debounceTimer);
    debounceTimer = null;
  }
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">SCHEDULES · 计划任务</p>
        <h1>计划任务</h1>
        <p class="head-note muted">
          cron 为 5 段：分 时 日 月 周。调度器在进程内按分钟轮询，到点即触发对应流程。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新增计划</el-button>
      </div>
    </header>

    <el-alert
      v-if="engine.online === false"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：计划任务不可用。启动 Python 后端后重试。"
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
      type="info"
      show-icon
      :closable="false"
      title="计划只在空闲时触发：若已有一次运行在执行，本次触发直接跳过，不会排队补跑。上面「因忙跳过」计数就是在记这件事。"
    />

    <el-alert
      v-if="standby"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      :title="`本进程不调度：${standbyReason || '另一个进程持有调度锁'}`"
      description="同一个数据库同时只允许一个进程轮询计划，这是刻意的：多个进程各自调度会让同一条 cron 触发多次，并重复截图。计划仍会按 cron 触发，只是由那个进程执行。"
    />

    <section class="card band">
      <div class="band-head">
        <p class="eyebrow">SCHEDULER · 调度器</p>
        <span class="state" :class="{ live: schedulerRunning, hold: standby }">
          <span class="dot" />
          {{ schedulerRunning ? "轮询中" : standby ? `由 PID ${leasePid ?? "?"} 调度` : "未运行" }}
        </span>
      </div>
      <dl class="band-grid">
        <div class="band-cell">
          <dt>轮询次数</dt>
          <dd class="mono">{{ statNumber("ticks") }}</dd>
        </div>
        <div class="band-cell">
          <dt>已触发</dt>
          <dd class="mono green">{{ statNumber("fired") }}</dd>
        </div>
        <div class="band-cell">
          <dt>因忙跳过</dt>
          <dd class="mono warn">{{ statNumber("skipped_busy") }}</dd>
        </div>
        <div class="band-cell">
          <dt>因已被占用跳过</dt>
          <dd class="mono warn">{{ statNumber("skipped_claimed") }}</dd>
        </div>
        <div class="band-cell">
          <dt>失败</dt>
          <dd class="mono">{{ statNumber("errors") }}</dd>
        </div>
        <div class="band-cell">
          <dt>最近轮询</dt>
          <dd class="mono">{{ timeAgoEpoch(Number(stats.last_tick_at) || 0) }}</dd>
        </div>
        <div class="band-cell">
          <dt>最近错误</dt>
          <dd class="mono err">{{ statText("last_error") || "无" }}</dd>
        </div>
      </dl>
    </section>

    <section class="card table-card">
      <el-table
        v-loading="loading"
        :data="schedules"
        row-key="id"
        size="small"
        class="schedules"
        empty-text="暂无计划任务"
      >
        <el-table-column label="名称" min-width="150">
          <template #default="scope">
            <div class="cell-name">
              <span class="name">{{ scope.row.name }}</span>
              <span class="fid mono">{{ scope.row.id }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="关联流程" min-width="150">
          <template #default="scope">
            <span class="flow-name">{{ scope.row.flow_name || scope.row.flow_id }}</span>
          </template>
        </el-table-column>

        <el-table-column label="cron" width="150">
          <template #default="scope">
            <span class="mono cron">{{ scope.row.cron }}</span>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="68" align="center">
          <template #default="scope">
            <el-switch
              :model-value="scope.row.enabled === 1"
              size="small"
              :loading="busy === `${scope.row.id}:toggle`"
              @update:model-value="toggle(scope.row, $event)"
            />
          </template>
        </el-table-column>

        <el-table-column label="下次触发" width="112">
          <template #default="scope">
            <span class="mono when">{{ nextFire(scope.row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上次运行" width="150">
          <template #default="scope">
            <div class="cell-last">
              <span class="mono when">{{ timeAgo(scope.row.last_run_at) }}</span>
              <el-tag size="small" :type="statusOf(scope.row.last_status).type" disable-transitions>
                {{ statusOf(scope.row.last_status).label }}
              </el-tag>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="118" align="right">
          <template #default="scope">
            <el-button size="small" link type="primary" :icon="EditPen" @click="openEdit(scope.row)">
              编辑
            </el-button>
            <el-popconfirm
              :title="`删除计划「${scope.row.name}」？`"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              :width="220"
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

    <el-dialog v-model="dialogOpen" :title="editing ? '编辑计划' : '新增计划'" width="500px" append-to-body>
      <el-form label-width="86px" size="small" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：工作日每 15 分钟" maxlength="60" />
        </el-form-item>
        <el-form-item label="关联流程">
          <el-select v-model="form.flow_id" class="full" placeholder="选择要触发的流程">
            <el-option v-for="item in flow.flows" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="cron">
          <el-input v-model="form.cron" placeholder="分 时 日 月 周，例如 */15 8-20 * * 1-5" @input="onCronInput" />
          <div class="preview">
            <el-tag
              size="small"
              :type="previewBusy ? 'info' : cronValid ? 'success' : 'danger'"
              disable-transitions
            >
              <el-icon v-if="previewBusy" class="is-loading"><AlarmClock /></el-icon>
              <template v-else>{{ preview ? (cronValid ? "表达式合法" : "表达式非法") : "未校验" }}</template>
            </el-tag>
            <span v-if="preview && !cronValid && preview.error" class="preview-error mono">{{ preview.error }}</span>
            <span v-else-if="preview?.fields" class="preview-fields mono">{{ preview.fields }}</span>
          </div>
          <ul v-if="cronValid && nextRuns.length" class="next-runs mono">
            <li v-for="when in nextRuns" :key="when">{{ moment(when) }}</li>
          </ul>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <p v-if="saveError" class="dialog-error mono">{{ saveError }}</p>
      <template #footer>
        <el-button size="small" @click="dialogOpen = false">取消</el-button>
        <el-button size="small" type="primary" :loading="saving" :disabled="!canSave" @click="save">
          保存
        </el-button>
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
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.notice {
  width: auto;
}

.band {
  padding: 14px 18px 16px;
}

.band-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.band-head .eyebrow {
  margin: 0 0 10px;
}

.state {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 10px;
  margin-bottom: 10px;
}

.state .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--muted);
}

.state.live {
  color: var(--green);
}

.state.live .dot {
  background: var(--green);
}

/* Standby is neither green nor red: this process is healthy, it just is not
   the one driving. Orange says "look over there"; red would send the operator
   hunting a fault that is not in this process. */
.state.hold {
  color: var(--orange);
}

.state.hold .dot {
  background: var(--orange);
}

.band-grid {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 14px;
}

.band-cell dt {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.6px;
}

.band-cell dd {
  margin: 6px 0 0;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.band-cell dd.green {
  color: var(--green);
}

.band-cell dd.warn {
  color: var(--orange);
}

.band-cell dd.err {
  color: var(--orange);
  font-size: 11px;
}

.table-card {
  padding: 0;
  overflow: hidden;
}

.schedules :deep(.el-table__cell) {
  padding: 8px 0;
}

.cell-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.name {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fid {
  font-size: 10px;
  color: var(--muted);
}

.flow-name {
  font-size: 12px;
}

.cron {
  font-size: 11px;
}

.when {
  font-size: 11px;
  color: var(--muted);
}

.cell-last {
  display: flex;
  align-items: center;
  gap: 8px;
}

.full {
  width: 100%;
}

.preview {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  margin-top: 7px;
  flex-wrap: wrap;
}

.preview-error {
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}

.preview-fields {
  color: var(--muted);
  font-size: 10.5px;
}

.next-runs {
  margin: 7px 0 0;
  padding-left: 16px;
  width: 100%;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.7;
}

.dialog-error {
  margin: 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}

@media (max-width: 1100px) {
  .band-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
