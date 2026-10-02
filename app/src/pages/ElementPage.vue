<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Aim, Delete, Plus, Refresh, Search } from "@element-plus/icons-vue";

import ElementPickerDialog from "../components/ElementPickerDialog.vue";

import { api, ApiError } from "../api/client";
import { useEngineStore } from "../stores/engine";
import { useFlowStore } from "../stores/flow";
import type { Element, Located } from "../types";

const DASH = "—";

type ElementKind = Element["kind"];
type TagType = "success" | "info" | "warning" | "danger";

const KIND_LABEL: Record<ElementKind, string> = {
  rect: "矩形",
  ocr: "文字锚点",
  image: "图像匹配",
  web: "网页选择器",
};

const KIND_HINT: Record<ElementKind, string> = {
  rect: "按窗口内绝对坐标解析，最快最稳；窗口移动后需要重新采集。",
  ocr: "在窗口内截图后做文字匹配，找到锚点文字再算偏移。",
  image: "模板匹配，需要 image_path 指向的模板图。",
  web: "通过 selector 在浏览器页面内取 bounds，需要先采集一次。",
};

const RELATIVE_TO: { value: "window" | "screen"; label: string }[] = [
  { value: "window", label: "窗口内坐标（相对微信窗口左上角）" },
  { value: "screen", label: "屏幕坐标（相对主屏左上角）" },
];

const engine = useEngineStore();
const flow = useFlowStore();

const elements = ref<Element[]>([]);
const loading = ref(false);
const loadError = ref("");
const rowError = ref("");
const busy = ref("");

/** Dry-resolve result per element name — the API is keyed by name, not id. */
const resolved = ref<Record<string, Located>>({});

const createOpen = ref(false);
const pickerOpen = ref(false);
const saving = ref(false);
const saveError = ref("");
const formName = ref("");
const formKind = ref<ElementKind>("rect");
const formX = ref(0);
const formY = ref(0);
const formWidth = ref(100);
const formHeight = ref(30);
const formRelative = ref<"window" | "screen">("window");
const formOcr = ref("");
const formFlowId = ref("");

function reason(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** Local-time stamp without a zone — see the same helper in FlowListPage. */
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

function rectText(element: Element): string {
  const rect = element.rect;
  return `${rect.x}, ${rect.y} · ${rect.width}×${rect.height}`;
}

/** The el-table slot hands out `any` rows; this keeps the lookup typed. */
function kindLabel(kind: ElementKind): string {
  return KIND_LABEL[kind];
}

function flowName(flowId: string | null): string {
  if (!flowId) return DASH;
  return flow.flows.find((item) => item.id === flowId)?.name ?? flowId;
}

function resultOf(name: string): Located | null {
  return resolved.value[name] ?? null;
}

function sourceTag(source: Located["source"]): { label: string; type: TagType } {
  if (source === "element") return { label: "元素库", type: "success" };
  if (source === "vision") return { label: "多模态", type: "warning" };
  return { label: DASH, type: "info" };
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    elements.value = (await api.elements()).elements;
  } catch (error) {
    loadError.value = reason(error);
  } finally {
    loading.value = false;
  }
}

/**
 * Resolve without clicking. This is the check an operator runs after moving or
 * resizing the WeChat window: it reports the point each element currently
 * points at, so a stale rectangle shows up before a run does.
 */
async function resolve(element: Element): Promise<void> {
  busy.value = `${element.id}:resolve`;
  rowError.value = "";
  try {
    const result = await api.resolveElement(element.name);
    resolved.value = { ...resolved.value, [element.name]: result };
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

async function remove(element: Element): Promise<void> {
  busy.value = `${element.id}:del`;
  rowError.value = "";
  try {
    await api.deleteElement(element.id);
    const next = { ...resolved.value };
    delete next[element.name];
    resolved.value = next;
    await load();
  } catch (error) {
    rowError.value = reason(error);
  } finally {
    busy.value = "";
  }
}

/** A freshly picked element is already stored; just show it in the list. */
function onPicked(): void {
  void load();
}

function openCreate(): void {
  formName.value = "";
  formKind.value = "rect";
  formX.value = 0;
  formY.value = 0;
  formWidth.value = 100;
  formHeight.value = 30;
  formRelative.value = "window";
  formOcr.value = "";
  formFlowId.value = "";
  saveError.value = "";
  createOpen.value = true;
}

async function save(): Promise<void> {
  const name = formName.value.trim();
  if (!name) {
    saveError.value = "名称不能为空";
    return;
  }
  saving.value = true;
  saveError.value = "";
  try {
    await api.saveElement({
      name,
      kind: formKind.value,
      rect: {
        x: formX.value,
        y: formY.value,
        width: formWidth.value,
        height: formHeight.value,
        relative_to: formRelative.value,
      },
      ocr_text: formOcr.value.trim(),
      flow_id: formFlowId.value || null,
    });
    createOpen.value = false;
    await load();
  } catch (error) {
    saveError.value = reason(error);
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  void load();
  void flow.loadFlows().catch(() => undefined);
});
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">ELEMENTS · 元素库</p>
        <h1>元素库</h1>
        <p class="head-note muted">
          路径A：在本地把坐标、OCR 锚点或模板图解析成一个点，不调用视觉模型、不产生费用、结果确定。
          路径B 多模态把整张截图交给模型，适配没有可命名结构的界面。
          「解析」只报告当前坐标，不会点击 —— 移动或缩放微信窗口后用它检查元素是否还准。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button :icon="Aim" size="small" @click="pickerOpen = true">拾取元素</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新增元素</el-button>
      </div>
    </header>

    <el-alert
      v-if="!engine.online"
      class="notice"
      type="warning"
      show-icon
      :closable="false"
      title="本地服务离线：元素库不可用。启动 Python 后端后重试。"
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

    <section class="card table-card">
      <el-table
        v-loading="loading"
        :data="elements"
        row-key="id"
        size="small"
        class="elements"
        empty-text="元素库为空：在设计器里用 vlm_describe 沉淀，或点右上角「新增元素」手工登记。"
      >
        <el-table-column label="名称" min-width="170">
          <template #default="scope">
            <div class="cell-name">
              <span class="name">{{ scope.row.name }}</span>
              <span class="fid mono">{{ scope.row.id }} · {{ flowName(scope.row.flow_id) }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="96">
          <template #default="scope">
            <el-tag size="small" type="info" disable-transitions>{{ kindLabel(scope.row.kind) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="区域 (x, y, w×h)" width="170">
          <template #default="scope">
            <div class="cell-rect">
              <span class="mono">{{ rectText(scope.row) }}</span>
              <span class="rel mono">{{ scope.row.rect.relative_to }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="OCR 锚点" min-width="120">
          <template #default="scope">
            <span class="mono ocr">{{ scope.row.ocr_text || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="98" align="right">
          <template #default="scope">
            <span class="mono when">{{ timeAgo(scope.row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="解析结果" min-width="210">
          <template #default="scope">
            <div v-if="resultOf(scope.row.name)" class="resolve">
              <template v-if="resultOf(scope.row.name)?.resolved">
                <div class="resolve-line">
                  <span class="mono point">
                    ({{ resultOf(scope.row.name)?.x }}, {{ resultOf(scope.row.name)?.y }})
                  </span>
                  <el-tag size="small" :type="sourceTag(resultOf(scope.row.name)?.source).type" disable-transitions>
                    {{ sourceTag(resultOf(scope.row.name)?.source).label }}
                  </el-tag>
                  <span class="mono conf">
                    置信度
                    {{ resultOf(scope.row.name)?.confidence?.toFixed(2) ?? DASH }}
                  </span>
                </div>
                <p class="resolve-note muted">
                  <template v-if="resultOf(scope.row.name)?.mode">
                    模式 {{ resultOf(scope.row.name)?.mode }}
                  </template>
                  <template v-if="resultOf(scope.row.name)?.label">
                    · {{ resultOf(scope.row.name)?.label }}
                  </template>
                </p>
              </template>
              <p v-else class="resolve-error mono">{{ resultOf(scope.row.name)?.error || "解析失败" }}</p>
            </div>
            <span v-else class="muted none">未解析</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="126" align="right">
          <template #default="scope">
            <el-button
              size="small"
              :icon="Search"
              :loading="busy === `${scope.row.id}:resolve`"
              @click="resolve(scope.row)"
            >
              解析
            </el-button>
            <el-popconfirm
              :title="`删除元素「${scope.row.name}」？引用该名称的定位节点会失败。`"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              :width="260"
              @confirm="remove(scope.row)"
            >
              <template #reference>
                <el-button
                  size="small"
                  type="danger"
                  plain
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

    <el-dialog v-model="createOpen" title="新增元素" width="520px" append-to-body>
      <el-form label-width="86px" size="small" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="formName" placeholder="定位节点 params.element 里引用的名字" maxlength="60" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="formKind" class="full">
            <el-option
              v-for="(label, kind) in KIND_LABEL"
              :key="kind"
              :label="label"
              :value="kind"
            />
          </el-select>
          <p class="field-help muted">{{ KIND_HINT[formKind] }}</p>
        </el-form-item>
        <el-form-item label="坐标">
          <div class="quad">
            <label><span>x</span><el-input-number v-model="formX" size="small" :step="1" /></label>
            <label><span>y</span><el-input-number v-model="formY" size="small" :step="1" /></label>
            <label><span>w</span><el-input-number v-model="formWidth" size="small" :step="1" /></label>
            <label><span>h</span><el-input-number v-model="formHeight" size="small" :step="1" /></label>
          </div>
        </el-form-item>
        <el-form-item label="坐标系">
          <el-select v-model="formRelative" class="full">
            <el-option v-for="option in RELATIVE_TO" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="formKind === 'ocr'" label="锚点文字">
          <el-input v-model="formOcr" placeholder="在窗口内截图里出现的文字，例如「搜索」" maxlength="80" />
        </el-form-item>
        <el-form-item label="关联流程">
          <el-select v-model="formFlowId" class="full" clearable placeholder="不限定，任意流程可用">
            <el-option v-for="item in flow.flows" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <p v-if="saveError" class="dialog-error mono">{{ saveError }}</p>
      <template #footer>
        <el-button size="small" @click="createOpen = false">取消</el-button>
        <el-button size="small" type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <ElementPickerDialog
      v-model="pickerOpen"
      :flow-id="null"
      @picked="onPicked"
    />
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

.table-card {
  padding: 0;
  overflow: hidden;
}

.elements :deep(.el-table__cell) {
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
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-rect {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
}

.rel {
  font-size: 10px;
  color: var(--muted);
}

.ocr {
  font-size: 11px;
  color: var(--muted);
  word-break: break-all;
}

.when {
  font-size: 11px;
  color: var(--muted);
}

.none {
  font-size: 11px;
}

.resolve {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.resolve-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.point {
  font-size: 12px;
}

.conf {
  font-size: 10px;
  color: var(--muted);
}

.resolve-note {
  margin: 0;
  font-size: 10px;
}

.resolve-error {
  margin: 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}

.full {
  width: 100%;
}

.quad {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  width: 100%;
}

.quad label {
  display: flex;
  align-items: center;
  gap: 5px;
}

.quad span {
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 10px;
}

.quad :deep(.el-input-number) {
  width: 100%;
}

.field-help {
  margin: 6px 0 0;
  font-size: 10.5px;
  line-height: 1.6;
  width: 100%;
}

.dialog-error {
  margin: 0;
  color: var(--orange);
  font-size: 11px;
  word-break: break-all;
}
</style>
