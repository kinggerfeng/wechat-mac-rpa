<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Delete, EditPen, Plus, Refresh, Cpu } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { api, ApiError } from "../api/client";
import type { LLMProvider } from "../types";

const DASH = "—";

interface ProviderForm {
  id: string | null;
  name: string;
  base_url: string;
  /** Only sent when the operator actually typed one; never the mask. */
  api_key: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  timeout: number | null;
  is_default: boolean;
  enabled: boolean;
  note: string;
}

const providers = ref<LLMProvider[]>([]);
const loading = ref(false);
const loadError = ref("");
const busy = ref("");

const dialogOpen = ref(false);
const saving = ref(false);
const saveError = ref("");
/** Whether the stored row already has a key, so the form can show it. */
const keyStored = ref(false);
const form = ref<ProviderForm>(emptyForm());

function emptyForm(): ProviderForm {
  return {
    id: null, name: "", base_url: "", api_key: "", model: "",
    temperature: null, max_tokens: null, timeout: null,
    is_default: false, enabled: true, note: "",
  };
}

const editing = computed(() => form.value.id !== null);
const canSave = computed(
  () => form.value.name.trim() !== "" && form.value.base_url.trim() !== "" && !saving.value,
);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await api.providers();
    providers.value = res.providers;
  } catch (e) {
    loadError.value = e instanceof ApiError ? String(e.detail ?? e.message) : String(e);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  form.value = emptyForm();
  keyStored.value = false;
  saveError.value = "";
  dialogOpen.value = true;
}

function openEdit(p: LLMProvider) {
  form.value = {
    id: p.id,
    name: p.name,
    base_url: p.base_url,
    // Never pre-fill the key field with the mask: saving would then write the
    // mask through as the key. An empty field means "keep the stored one".
    api_key: "",
    model: p.model,
    temperature: p.temperature,
    max_tokens: p.max_tokens,
    timeout: p.timeout,
    is_default: p.is_default === 1,
    enabled: p.enabled === 1,
    note: p.note,
  };
  keyStored.value = p.api_key_set;
  saveError.value = "";
  dialogOpen.value = true;
}

async function save() {
  if (!canSave.value) return;
  saving.value = true;
  saveError.value = "";
  try {
    const payload: Partial<LLMProvider> = {
      id: form.value.id ?? undefined,
      name: form.value.name.trim(),
      base_url: form.value.base_url.trim(),
      model: form.value.model.trim(),
      temperature: form.value.temperature,
      max_tokens: form.value.max_tokens,
      timeout: form.value.timeout,
      is_default: form.value.is_default ? 1 : 0,
      enabled: form.value.enabled ? 1 : 0,
      note: form.value.note,
    };
    // Omit the key entirely unless one was typed. Sending "" would be read
    // as "keep the stored one" server-side, but omitting it is unambiguous.
    if (form.value.api_key.trim()) payload.api_key = form.value.api_key.trim();
    await api.saveProvider(payload);
    ElMessage.success(form.value.id ? "已保存" : "已添加");
    dialogOpen.value = false;
    await load();
  } catch (e) {
    saveError.value = e instanceof ApiError ? String(e.detail ?? e.message) : String(e);
  } finally {
    saving.value = false;
  }
}

async function remove(p: LLMProvider) {
  try {
    await ElMessageBox.confirm(
      `确定删除 provider「${p.name}」？使用该 provider 的流程会改为使用默认项。`,
      "删除确认",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return; // user cancelled
  }
  busy.value = p.id;
  try {
    await api.deleteProvider(p.id);
    ElMessage.success("已删除");
    await load();
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? String(e.detail ?? e.message) : String(e));
  } finally {
    busy.value = "";
  }
}

/** Sends a real request so a typo'd base_url surfaces here, not mid-run. */
async function test(p: LLMProvider) {
  busy.value = p.id;
  try {
    const res = await api.testProvider(p.id);
    ElMessage.success(`连通正常：${res.reply || "(空回复)"}`);
  } catch (e) {
    ElMessage.error(e instanceof ApiError ? String(e.detail ?? e.message) : String(e));
  } finally {
    busy.value = "";
    await load(); // pick up the recorded last_ok_at / last_error
  }
}

function healthTag(p: LLMProvider): { text: string; type: "success" | "danger" | "info" } {
  if (p.last_error) return { text: "异常", type: "danger" };
  if (p.last_ok_at) return { text: "正常", type: "success" };
  return { text: "未测试", type: "info" };
}

onMounted(load);
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>大模型网关</h2>
        <p class="sub">
          在这里配置流程可用的 LLM 网关。密钥存于本地 rpa.db（混淆存储，不进版本库），
          流程里的「llm」节点默认使用标记为默认的 provider。
        </p>
      </div>
      <div class="actions">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">添加网关</el-button>
      </div>
    </header>

    <el-alert v-if="loadError" type="error" :closable="false" :title="loadError" class="mb" />

    <el-empty v-if="!loading && providers.length === 0" description="尚未配置任何网关">
      <el-button type="primary" :icon="Plus" @click="openCreate">添加网关</el-button>
    </el-empty>

    <el-table v-else :data="providers" v-loading="loading" stripe>
      <el-table-column label="名称" min-width="150">
        <template #default="{ row }">
          <div class="cell-stack">
            <span>{{ row.name }}</span>
            <div class="tags">
              <el-tag v-if="row.is_default === 1" size="small" type="primary">默认</el-tag>
              <el-tag v-if="row.enabled !== 1" size="small" type="info">已停用</el-tag>
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="地址" min-width="260">
        <template #default="{ row }">
          <div class="cell-stack">
            <code class="mono">{{ row.base_url }}</code>
            <span class="muted">{{ row.model || DASH }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="密钥" width="140">
        <template #default="{ row }">
          <code v-if="row.api_key_set" class="mono">{{ row.api_key }}</code>
          <span v-else class="muted">未配置</span>
        </template>
      </el-table-column>

      <el-table-column label="连通性" width="110">
        <template #default="{ row }">
          <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
            <el-tag size="small" :type="healthTag(row).type">{{ healthTag(row).text }}</el-tag>
          </el-tooltip>
          <el-tag v-else size="small" :type="healthTag(row).type">{{ healthTag(row).text }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="210" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Cpu" :loading="busy === row.id" @click="test(row)">测试</el-button>
          <el-button size="small" :icon="EditPen" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" :loading="busy === row.id" @click="remove(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogOpen"
      :title="editing ? '编辑网关' : '添加网关'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form label-width="100px" label-position="left">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例：mimo 官方" />
        </el-form-item>

        <el-form-item label="接口地址" required>
          <el-input v-model="form.base_url" placeholder="https://host/v1" />
          <div class="hint">OpenAI 兼容端点。填到 /v1 为止即可。</div>
        </el-form-item>

        <el-form-item label="密钥">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="keyStored ? '留空则不修改' : 'sk-...'"
          />
          <div v-if="keyStored" class="hint">已配置密钥，留空保存将保持不变。</div>
        </el-form-item>

        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="例：mimo-v2.5" />
        </el-form-item>

        <el-form-item label="默认参数">
          <div class="inline-fields">
            <el-input-number v-model="form.temperature" :controls="false" :step="0.05" placeholder="温度" />
            <el-input-number v-model="form.max_tokens" :controls="false" :step="128" placeholder="max_tokens" />
            <el-input-number v-model="form.timeout" :controls="false" :step="5" placeholder="超时(秒)" />
          </div>
          <div class="hint">留空则流程节点上的同名参数优先；都为空则用客户端默认。</div>
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="form.note" placeholder="可选" />
        </el-form-item>

        <el-form-item label="选项">
          <el-checkbox v-model="form.is_default">设为默认网关</el-checkbox>
          <el-checkbox v-model="form.enabled">启用</el-checkbox>
        </el-form-item>
      </el-form>

      <el-alert v-if="saveError" type="error" :closable="false" :title="saveError" class="mb" />

      <template #footer>
        <el-button @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSave" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 4px; font-size: 18px; }
.sub { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; max-width: 640px; }
.actions { display: flex; gap: 8px; flex-shrink: 0; }
.mb { margin-bottom: 12px; }
.cell-stack { display: flex; flex-direction: column; gap: 2px; }
.tags { display: flex; gap: 4px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; margin-top: 2px; }
.inline-fields { display: flex; gap: 8px; }
</style>
