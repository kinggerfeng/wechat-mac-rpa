<script setup lang="ts">
// Right-hand inspector for the selected node.
//
// The path selector is repeated here as a full control with its consequences
// spelled out, because the badge on the node tells you *what* is chosen and this
// panel has to tell you *what it costs*. A designer switching a node to `auto`
// should see, in the same glance, that a missing element will now call a model.

import { computed, toRef } from "vue";
import VariablePicker from "./VariablePicker.vue";
import { useVariableCatalog } from "../../composables/useVariableCatalog";
import type { FlowEdge, FlowGraph, FlowNode, LocatePath, NodeSpec, ValidationIssue } from "../../types";
import { LOCATE_PATHS, PATH_HINTS, PATH_LABELS } from "../../types";

const props = defineProps<{
  node: FlowNode | null;
  spec?: NodeSpec;
  effective: LocatePath | null;
  issues: ValidationIssue[];
  incoming: FlowEdge[];
  targets: string[];
  graph: FlowGraph;
  specFor: (type: string) => NodeSpec | undefined;
}>();

const emit = defineEmits<{
  (e: "patch", patch: Partial<FlowNode>): void;
  (e: "set-path", path: LocatePath | null): void;
  (e: "remove"): void;
  (e: "open-element"): void;
}>();

const paramValue = (name: string): unknown => props.node?.params?.[name];

function setParam(name: string, value: unknown) {
  if (!props.node) return;
  emit("patch", {
    params: { ...props.node.params, [name]: value },
  });
}

const visionCost = computed(
  () => props.effective === "auto" || props.effective === "vision",
);

// Which values are actually in scope for the node being edited. Computed from
// the graph, not from the last run, because the graph is what the author is
// changing and a stale trace would offer names this edit just removed.
const catalog = useVariableCatalog(
  toRef(props, "graph"),
  toRef(props, "node"),
  (type) => props.specFor(type),
);

const VARIABLE_HINT = "可直接输入，或点右侧按钮从上游变量里挑";
</script>

<template>
  <aside class="inspector">
    <div v-if="!node" class="empty">
      <span class="empty-mark">◇</span>
      <p>选中一个节点以编辑参数</p>
    </div>

    <template v-else>
      <header class="insp-head">
        <div>
          <p class="eyebrow">{{ spec?.label ?? node.type }}</p>
          <input
            class="name-input"
            :value="node.name"
            aria-label="节点名称"
            @input="emit('patch', { name: ($event.target as HTMLInputElement).value })"
          />
        </div>
        <el-button size="small" text type="danger" @click="emit('remove')">删除</el-button>
      </header>

      <!-- ── 双路径选择 ── -->
      <section v-if="spec?.path_aware" class="section path-section">
        <h3 class="section-title">
          定位路径
          <el-tag size="small" :type="visionCost ? 'warning' : 'success'" effect="plain">
            {{ effective ? PATH_LABELS[effective] : "未选定" }}
          </el-tag>
        </h3>
        <p class="section-note">
          在配置流程时选定，运行时不再改变。元素库命中是本地、确定、免费的；多模态每次定位都会调用视觉模型。
        </p>

        <el-radio-group
          :model-value="node.path ?? ''"
          class="path-options"
          @update:model-value="emit('set-path', ($event as LocatePath) || null)"
        >
          <el-radio
            v-for="path in LOCATE_PATHS"
            :key="path"
            :value="path"
            :label="path"
          >
            <span class="opt-label">{{ PATH_LABELS[path] }}</span>
            <span class="opt-hint">{{ PATH_HINTS[path] }}</span>
          </el-radio>
        </el-radio-group>

        <el-checkbox
          :model-value="!node.path"
          @update:model-value="emit('set-path', $event ? null : (effective ?? 'element'))"
        >
          继承流程默认路径
        </el-checkbox>

        <div class="target-row">
          <span class="field-label">目标应用</span>
          <el-select
            :model-value="node.target ?? ''"
            size="small"
            placeholder="继承"
            clearable
            @update:model-value="emit('patch', { target: ($event as string) || null })"
          >
            <el-option v-for="name in targets" :key="name" :label="name" :value="name" />
          </el-select>
        </div>
      </section>

      <!-- ── 参数 ── -->
      <section v-if="spec?.params.length" class="section">
        <h3 class="section-title">参数</h3>
        <div v-for="param in spec.params" :key="param.name" class="field">
          <label class="field-label">
            {{ param.label }}
            <span v-if="param.required" class="required">必填</span>
          </label>

          <el-input
            v-if="param.kind === 'text'"
            :model-value="String(paramValue(param.name) ?? '')"
            size="small"
            :placeholder="param.help"
            @update:model-value="setParam(param.name, $event)"
          />
          <VariablePicker
            v-else-if="param.kind === 'variable'"
            :model-value="String(paramValue(param.name) ?? '')"
            :placeholder="param.help || VARIABLE_HINT"
            :refs="catalog.refs.value"
            :ambiguous="catalog.ambiguous.value"
            @update:model-value="setParam(param.name, $event)"
          />
          <el-input
            v-else-if="param.kind === 'textarea'"
            :model-value="String(paramValue(param.name) ?? '')"
            type="textarea"
            :rows="3"
            size="small"
            :placeholder="param.help"
            @update:model-value="setParam(param.name, $event)"
          />
          <el-input-number
            v-else-if="param.kind === 'number'"
            :model-value="Number(paramValue(param.name) ?? 0)"
            size="small"
            controls-position="right"
            @update:model-value="setParam(param.name, $event)"
          />
          <el-switch
            v-else-if="param.kind === 'bool'"
            :model-value="Boolean(paramValue(param.name))"
            @update:model-value="setParam(param.name, $event)"
          />
          <el-select
            v-else-if="param.kind === 'select'"
            :model-value="String(paramValue(param.name) ?? '')"
            size="small"
            @update:model-value="setParam(param.name, $event)"
          >
            <el-option
              v-for="choice in param.choices"
              :key="String(choice)"
              :label="String(choice)"
              :value="choice"
            />
          </el-select>

          <p v-if="param.help" class="field-help">{{ param.help }}</p>
        </div>
      </section>

      <!-- ── 执行策略 ── -->
      <section class="section">
        <h3 class="section-title">执行策略</h3>
        <div class="field">
          <label class="field-label">失败时</label>
          <el-select
            :model-value="node.on_error"
            size="small"
            @update:model-value="emit('patch', { on_error: $event as FlowNode['on_error'] })"
          >
            <el-option label="终止流程" value="fail" />
            <el-option label="走 error 分支" value="branch" />
            <el-option label="忽略并继续" value="ignore" />
          </el-select>
        </div>
        <div class="field-row">
          <div class="field">
            <label class="field-label">重试次数</label>
            <el-input-number
              :model-value="node.retry.max"
              :min="0"
              :max="10"
              size="small"
              controls-position="right"
              @update:model-value="emit('patch', { retry: { ...node.retry, max: Number($event) } })"
            />
          </div>
          <div class="field">
            <label class="field-label">超时(秒)</label>
            <el-input-number
              :model-value="node.timeout ?? 0"
              :min="0"
              size="small"
              controls-position="right"
              @update:model-value="emit('patch', { timeout: Number($event) || null })"
            />
          </div>
        </div>
        <el-checkbox
          :model-value="node.disabled"
          @update:model-value="emit('patch', { disabled: Boolean($event) })"
        >
          停用此节点
        </el-checkbox>
      </section>

      <!-- ── 入边 ── -->
      <section class="section">
        <h3 class="section-title">入边 ({{ incoming.length }})</h3>
        <p v-if="!incoming.length" class="section-note">没有入边，该节点只能作为入口。</p>
        <div v-for="edge in incoming" :key="edge.id" class="incoming">
          <span class="mono">{{ edge.source }}</span>
          <span class="port-tag">{{ edge.source_port }}</span>
          <el-button size="small" text type="danger" @click="emit('patch', {})">×</el-button>
        </div>
      </section>

      <!-- ── 校验问题 ── -->
      <section v-if="issues.length" class="section">
        <h3 class="section-title">校验 ({{ issues.length }})</h3>
        <div
          v-for="(issue, index) in issues"
          :key="index"
          class="issue"
          :class="issue.severity"
        >
          <span class="issue-code">{{ issue.code }}</span>
          <p>{{ issue.message }}</p>
        </div>
      </section>
    </template>
  </aside>
</template>

<style scoped>
.inspector {
  width: 300px;
  flex: 0 0 300px;
  min-width: 300px;
  overflow-y: auto;
  border-left: 1px solid var(--line);
  background: #fbfcfa;
}

.empty {
  padding: 60px 24px;
  color: #9aa59e;
  font-size: 11.5px;
  text-align: center;
}

.empty-mark {
  display: block;
  margin-bottom: 9px;
  color: #809087;
  font-size: 20px;
}

.insp-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 14px 11px;
  border-bottom: 1px solid var(--line);
}

.eyebrow {
  margin: 0 0 6px;
  color: #809087;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1.2px;
}

.name-input {
  width: 100%;
  border: 0;
  background: transparent;
  color: #24322d;
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 600;
  outline: none;
}

.section {
  padding: 13px 14px;
  border-bottom: 1px solid #e8ede9;
}

.path-section {
  background: linear-gradient(180deg, rgba(107, 91, 181, 0.045), transparent);
}

.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 8px;
  color: #35443e;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.4px;
}

.section-note {
  margin: 0 0 9px;
  color: #8a9790;
  font-size: 10.5px;
  line-height: 1.55;
}

.path-options {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 3px;
  width: 100%;
  margin-bottom: 7px;
}

.path-options :deep(.el-radio) {
  height: auto;
  margin: 0;
  padding: 5px 7px;
  border: 1px solid transparent;
  border-radius: var(--radius);
}

.path-options :deep(.el-radio:hover) {
  background: #f0f5f1;
}

.opt-label {
  display: block;
  color: #2c3a34;
  font-size: 11.5px;
  font-weight: 600;
}

.opt-hint {
  display: block;
  color: #93a098;
  font-size: 9.5px;
  line-height: 1.4;
}

.field {
  margin-bottom: 10px;
}

.field-row {
  display: flex;
  gap: 9px;
}

.field-row .field {
  flex: 1;
}

.field-label {
  display: block;
  margin-bottom: 4px;
  color: #6a7a72;
  font-size: 10.5px;
}

.required {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 2px;
  background: #f6e3da;
  color: #a4552f;
  font-size: 9px;
}

.field-help {
  margin: 3px 0 0;
  color: #9aa59e;
  font-size: 9.5px;
  line-height: 1.4;
}

.target-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
}

.target-row .field-label {
  margin: 0;
}

.incoming {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  color: #6a7a72;
  font-size: 10.5px;
}

.port-tag {
  padding: 0 5px;
  border-radius: 2px;
  background: #e8ede9;
  font-size: 9.5px;
}

.issue {
  margin-bottom: 6px;
  padding: 6px 8px;
  border-left: 2px solid;
  border-radius: 0 2px 2px 0;
  font-size: 10.5px;
  line-height: 1.45;
}

.issue p {
  margin: 2px 0 0;
  color: #5d6b64;
}

.issue-code {
  font-family: var(--font-mono);
  font-size: 9.5px;
}

.issue.error {
  border-color: #d98b6a;
  background: #fbf0e9;
}

.issue.error .issue-code { color: #a4552f; }

.issue.warning {
  border-color: #d8b95e;
  background: #fbf5e4;
}

.issue.warning .issue-code { color: #8a6420; }
</style>
