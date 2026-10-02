<script setup lang="ts">
// One node on the canvas.
//
// The path badge lives on the node body, not in the inspector, on purpose: the
// single most consequential decision in an RPA flow is which location strategy
// this step uses, and a designer scanning a large graph has to see it without
// clicking anything.

import { computed } from "vue";
import type { FlowNode, LocatePath, NodeSpec } from "../../types";
import { PATH_HINTS, PATH_LABELS } from "../../types";
import type { ValidationIssue } from "../../types";

const props = defineProps<{
  node: FlowNode;
  spec?: NodeSpec;
  selected: boolean;
  effective: LocatePath | null;
  issues: ValidationIssue[];
  running: boolean;
}>();

const emit = defineEmits<{
  (e: "select", id: string): void;
  (e: "remove", id: string): void;
  (e: "set-entry", id: string): void;
  (e: "set-path", id: string, path: LocatePath | null): void;
  (e: "port-down", id: string, port: string, event: MouseEvent): void;
}>();

const NODE_W = 208;

const pathClass = computed(() => `path-${props.effective ?? "none"}`);
const isPathAware = computed(() => props.spec?.path_aware === true);
const undecided = computed(() => isPathAware.value && !props.effective);

const hasError = computed(() => props.issues.some((i) => i.severity === "error"));
const hasWarning = computed(() => props.issues.some((i) => i.severity === "warning"));

/** Ports other than the default one; the default is drawn as a single stub. */
const extraPorts = computed(() => (props.spec?.outputs ?? []).filter((p) => p !== "ok"));

/** The select is a plain <select>, so its value is a string; narrow it back to
 *  the union the schema actually accepts rather than casting the whole node. */
function asPath(value: string): LocatePath | null {
  return (["element", "element_strict", "vision", "auto"] as const).find((p) => p === value) ?? null;
}

function startPort(event: MouseEvent, port: string) {
  event.stopPropagation();
  emit("port-down", props.node.id, port, event);
}
</script>

<template>
  <div
    class="node"
    :class="[
      pathClass,
      { selected, running, disabled: node.disabled, 'has-error': hasError },
    ]"
    :style="{ width: `${NODE_W}px` }"
    @click.stop="emit('select', node.id)"
  >
    <header class="node-head">
      <span class="node-kind">{{ spec?.label ?? node.type }}</span>
      <div class="node-actions">
        <button
          v-if="node.id !== undefined"
          class="icon-btn"
          title="设为入口节点"
          @click.stop="emit('set-entry', node.id)"
        >
          ▶
        </button>
        <button class="icon-btn danger" title="删除节点" @click.stop="emit('remove', node.id)">×</button>
      </div>
    </header>

    <p class="node-name">{{ node.name || node.type }}</p>

    <!-- The dual-path declaration, on the node itself. -->
    <div v-if="isPathAware" class="path-strip" :title="effective ? PATH_HINTS[effective] : '尚未选定路径'">
      <span class="path-dot" />
      <select
        class="path-select"
        :value="node.path ?? ''"
        @click.stop
        @change="emit('set-path', node.id, asPath(($event.target as HTMLSelectElement).value))
          "
      >
        <option value="">
          {{ node.path ? '' : `继承：${effective ? PATH_LABELS[effective] : '未设置'}` }}
        </option>
        <option value="element">路径A · {{ PATH_LABELS.element }}</option>
        <option value="element_strict">路径A · {{ PATH_LABELS.element_strict }}</option>
        <option value="vision">路径B · {{ PATH_LABELS.vision }}</option>
        <option value="auto">自动 · {{ PATH_LABELS.auto }}</option>
      </select>
      <span v-if="undecided" class="path-undecided">必选</span>
    </div>

    <footer class="node-foot">
      <span v-if="node.on_error !== 'fail'" class="chip chip-warn">
        {{ node.on_error === "branch" ? "失败转分支" : "忽略失败" }}
      </span>
      <span v-if="node.retry?.max" class="chip">重试 {{ node.retry.max }}</span>
      <span v-if="node.timeout" class="chip">{{ node.timeout }}s</span>
      <span v-if="hasError" class="chip chip-error">错误</span>
      <span v-else-if="hasWarning" class="chip chip-warn">警告</span>
    </footer>

    <!-- Ports. `ok` sits on the right (normal flow); the rest stack on the left
         so error/manual branches read as incoming side-rails. -->
    <button class="port port-out" title="ok" @mousedown="startPort($event, 'ok')" />
    <button
      v-for="port in extraPorts"
      :key="port"
      class="port port-in"
      :class="`port-${port}`"
      :title="port"
      @mousedown.stop="startPort($event, port)"
    />
  </div>
</template>

<style scoped>
.node {
  position: absolute;
  border: 1px solid var(--line);
  border-left-width: 3px;
  border-radius: var(--radius);
  background: var(--paper);
  box-shadow: 0 4px 14px rgba(29, 62, 49, 0.07);
  cursor: grab;
  user-select: none;
  transition: box-shadow 0.14s, border-color 0.14s;
}

.node:hover {
  box-shadow: 0 8px 22px rgba(29, 62, 49, 0.13);
}

.node.selected {
  box-shadow: 0 0 0 2px #357b63;
}

.node.running {
  animation: pulse 1.1s ease-in-out infinite;
}

.node.disabled {
  opacity: 0.5;
  filter: grayscale(0.5);
}

.node.has-error {
  border-color: #d98b6a;
}

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(82, 162, 124, 0.5); }
  50% { box-shadow: 0 0 0 6px rgba(82, 162, 124, 0); }
}

.node-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 9px 3px;
}

.node-kind {
  color: #7b8a83;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.6px;
}

.node-actions {
  display: flex;
  gap: 1px;
  opacity: 0;
  transition: opacity 0.12s;
}

.node:hover .node-actions {
  opacity: 1;
}

.icon-btn {
  width: 17px;
  height: 17px;
  border: 0;
  border-radius: 2px;
  background: transparent;
  color: #8a9790;
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
}

.icon-btn:hover {
  background: #e7ece8;
  color: #35443e;
}

.icon-btn.danger:hover {
  background: #f6e3da;
  color: #a4552f;
}

.node-name {
  margin: 0;
  padding: 0 9px 7px;
  color: #24322d;
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.path-strip {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0 7px 7px;
  padding: 3px 6px;
  border-radius: 2px;
  font-size: 10px;
}

.path-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: #9a7667;
}

.path-select {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 10px;
  cursor: pointer;
  outline: none;
}

.path-undecided {
  padding: 0 4px;
  border-radius: 2px;
  background: #f6e3da;
  color: #a4552f;
  font-size: 9px;
  font-weight: 600;
}

.path-element .path-strip { background: var(--path-element-soft); color: #24523f; }
.path-element .path-dot { background: var(--path-element); }
.path-element_strict .path-strip { background: var(--path-element-soft); color: #24523f; }
.path-element_strict .path-dot { background: var(--path-element); }
.path-vision .path-strip { background: var(--path-vision-soft); color: #443668; }
.path-vision .path-dot { background: var(--path-vision); }
.path-auto .path-strip { background: var(--path-auto-soft); color: #6d4d13; }
.path-auto .path-dot { background: var(--path-auto); }
.path-none .path-strip { background: #f1e3de; color: #8c4a30; }
.path-none .path-dot { background: #b5643c; }

.node-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  min-height: 15px;
  padding: 0 7px 7px;
}

.chip {
  padding: 1px 5px;
  border-radius: 2px;
  background: #e8ede9;
  color: #6a7a72;
  font-size: 9px;
  white-space: nowrap;
}

.chip-warn { background: #f7efdd; color: #8a6420; }
.chip-error { background: #f6e3da; color: #a4552f; }

.port {
  position: absolute;
  width: 9px;
  height: 9px;
  border: 1.5px solid #8fa79a;
  border-radius: 50%;
  background: var(--paper);
  padding: 0;
  cursor: crosshair;
  transition: transform 0.1s, background 0.1s;
}

.port:hover {
  transform: scale(1.4);
  background: #357b63;
}

.port-out {
  top: 50%;
  right: -5px;
  margin-top: -4px;
  border-color: var(--path-element);
}

.port-in {
  left: -5px;
  margin-top: -4px;
}

.port-in:nth-of-type(1) { top: 26%; }
.port-in:nth-of-type(2) { top: 72%; }
.port-in:nth-of-type(3) { top: 88%; }

.port-error { border-color: #d98b6a; }
.port-manual { border-color: #b5643c; }
.port-empty { border-color: #c0a95a; }
.port-false { border-color: #a0a0a0; }
</style>
