<script setup lang="ts">
// The canvas: a pan/zoom surface where a flow is authored.
//
// Interaction model kept deliberately plain so it is learnable in a minute:
// drag from the palette to drop a node, drag a node to move it, drag from a
// node's port to another node to connect, click to select, double-click the
// background to fit. Nothing is hidden behind a mode switch.

import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Back, FullScreen, Refresh, VideoPause, VideoPlay } from "@element-plus/icons-vue";

import EdgeLayer from "../components/canvas/EdgeLayer.vue";
import InspectorPanel from "../components/canvas/InspectorPanel.vue";
import NodeCard from "../components/canvas/NodeCard.vue";
import NodePalette from "../components/canvas/NodePalette.vue";
import TracePanel from "../components/canvas/TracePanel.vue";
import { api } from "../api/client";
import { useFlowStore } from "../stores/flow";
import { useTraceStore } from "../stores/trace";
import type { LocatePath } from "../types";

const route = useRoute();
const router = useRouter();
const flow = useFlowStore();
const trace = useTraceStore();

const surface = ref<HTMLElement | null>(null);

// fit() must not run on a half-laid-out surface. A single requestAnimationFrame
// measured the trace panel's collapsed height, so the centring maths ran against
// a box ~600px shorter than the real one and panned the graph off-screen while
// still reporting a plausible zoom. Observing the element is the only way to
// know when its box is actually final.
let surfaceObserver: ResizeObserver | null = null;
let fitPending = false;
// Once the user zooms or pans, a window resize must not yank the view back.
// Only an explicit "fit" and a freshly opened flow re-frame the graph.
const viewPinned = ref(false);

function scheduleFit(force = false): void {
  if (fitPending) return;
  fitPending = true;
  requestAnimationFrame(() => {
    fitPending = false;
    if (force || !viewPinned.value) fit();
  });
}
const selectedNodeId = ref<string | null>(null);
const selectedEdgeId = ref<string | null>(null);
const zoom = ref(1);
const pan = ref({ x: 0, y: 0 });
const showTrace = ref(true);
const dryRun = ref(true);
const runningFlow = ref(false);
const targets = ref<string[]>(["wechat", "any_window"]);

// The palette (216) + inspector (300) take 516px off the width. Below 1200
// that leaves the graph with less room than a node is wide, so both panels
// become overlay drawers and the surface keeps 100% of the width. Overlaying
// rather than squeezing is the point: the canvas is the work surface, and a
// panel that reclaims its space every time a node is selected makes the graph
// jump under the cursor.
const NARROW_AT = 1200;
const narrow = ref(false);
const paletteOpen = ref(false);
const inspectorOpen = ref(false);

function measure() {
  narrow.value = window.innerWidth < NARROW_AT;
  if (!narrow.value) {
    paletteOpen.value = false;
    inspectorOpen.value = false;
  } else {
    // Start with the palette open — it is what the user needs before they
    // have anything to inspect. Selecting a node then takes over.
    paletteOpen.value = !inspectorOpen.value && !selectedNodeId.value;
  }
}

type DragMode =
  | { kind: "none" }
  | { kind: "pan"; startX: number; startY: number; originX: number; originY: number }
  | { kind: "node"; id: string; dx: number; dy: number }
  | { kind: "link"; from: string; port: string; x: number; y: number }
  | { kind: "drop"; x: number; y: number };

const drag = ref<DragMode>({ kind: "none" });

const BOUNDS = { w: 6400, h: 4200 };

const selectedNode = computed(() =>
  selectedNodeId.value ? (flow.nodeById(selectedNodeId.value) ?? null) : null,
);

const incomingEdges = computed(() =>
  selectedNodeId.value
    ? flow.draft.edges.filter((e) => e.target === selectedNodeId.value)
    : [],
);

const runningNodeIds = computed(() =>
  trace.nodes.filter((n) => n.pending).map((n) => n.nodeId),
);

/** Vertical fan-out so several branches from one node do not overlap. */
const portOffsets = computed<Record<string, number>>(() => {
  const counts = new Map<string, number>();
  const out: Record<string, number> = {};
  for (const node of flow.draft.nodes) {
    const spec = flow.specFor(node.type);
    const ports = spec?.outputs ?? ["ok"];
    ports.forEach((port, index) => {
      out[`${node.id}:${port}`] = ports.length === 1 ? 0 : (index - (ports.length - 1) / 2) * 13;
      counts.set(`${node.id}:${port}`, (counts.get(`${node.id}:${port}`) ?? 0) + 1);
    });
  }
  return out;
});

const transform = computed(
  () => `translate(${pan.value.x}px, ${pan.value.y}px) scale(${zoom.value})`,
);

// ── coordinate helpers ──

function toCanvas(event: MouseEvent | DragEvent): { x: number; y: number } {
  const box = surface.value?.getBoundingClientRect();
  if (!box) return { x: 0, y: 0 };
  return {
    x: (event.clientX - box.left - pan.value.x) / zoom.value,
    y: (event.clientY - box.top - pan.value.y) / zoom.value,
  };
}

function fit(): void {
  const box = surface.value?.getBoundingClientRect();
  if (!box || !box.width || !box.height || !flow.draft.nodes.length) return;
  const xs = flow.draft.nodes.map((n) => n.position.x);
  const ys = flow.draft.nodes.map((n) => n.position.y);
  const minX = Math.min(...xs) - 40;
  const minY = Math.min(...ys) - 40;
  const maxX = Math.max(...xs) + 250;
  const maxY = Math.max(...ys) + 140;
  // The floor is 0.15, not 0.3: a 2510px-wide flow on a ~500px surface needs
  // 0.2 to fit, and clamping to 0.3 overflowed the surface and threw off the
  // centring below because pan was computed with a zoom the nodes don't have.
  const scale = Math.min(1.1, Math.max(0.15, Math.min(box.width / (maxX - minX), box.height / (maxY - minY))));
  zoom.value = Number(scale.toFixed(2));
  pan.value = {
    x: (box.width - (maxX - minX) * zoom.value) / 2 - minX * zoom.value,
    y: (box.height - (maxY - minY) * zoom.value) / 2 - minY * zoom.value,
  };
  viewPinned.value = false;
}

function zoomBy(factor: number): void {
  viewPinned.value = true;
  zoom.value = Number(Math.min(2, Math.max(0.15, zoom.value * factor)).toFixed(2));
}

// ── pointer handling ──

function onPointerDown(event: MouseEvent) {
  if (event.button === 1 || event.altKey) {
    viewPinned.value = true;
    drag.value = {
      kind: "pan",
      startX: event.clientX,
      startY: event.clientY,
      originX: pan.value.x,
      originY: pan.value.y,
    };
  } else if (event.target === surface.value) {
    selectedNodeId.value = null;
    selectedEdgeId.value = null;
  }
}

function startNodeDrag(event: MouseEvent, nodeId: string) {
  const node = flow.nodeById(nodeId);
  if (!node) return;
  event.stopPropagation();
  selectedNodeId.value = nodeId;
  selectedEdgeId.value = null;
  // On a narrow window the inspector is a drawer; selecting a node with no
  // visible response looks like the click did nothing.
  if (narrow.value) {
    paletteOpen.value = false;
    inspectorOpen.value = true;
  }
  const point = toCanvas(event);
  drag.value = { kind: "node", id: nodeId, dx: point.x - node.position.x, dy: point.y - node.position.y };
}

function startLink(nodeId: string, port: string, event: MouseEvent) {
  const point = toCanvas(event);
  drag.value = { kind: "link", from: nodeId, port, x: point.x, y: point.y };
}

function onPointerMove(event: MouseEvent) {
  const state = drag.value;
  if (state.kind === "pan") {
    pan.value = {
      x: state.originX + (event.clientX - state.startX),
      y: state.originY + (event.clientY - state.startY),
    };
  } else if (state.kind === "node") {
    const point = toCanvas(event);
    flow.moveNode(state.id, point.x - state.dx, point.y - state.dy);
  } else if (state.kind === "link") {
    const point = toCanvas(event);
    drag.value = { ...state, x: point.x, y: point.y };
  }
}

function onPointerUp(event: MouseEvent) {
  const state = drag.value;
  if (state.kind === "link") {
    // Resolve what is under the cursor. `closest` is scoped to the card's data
    // attribute so a drop on empty canvas silently cancels rather than
    // connecting to whatever happens to be under the pointer.
    const element = document.elementFromPoint(event.clientX, event.clientY);
    const host = element?.closest<HTMLElement>("[data-node-id]");
    const targetId = host?.dataset.nodeId;
    if (targetId && targetId !== state.from) {
      flow.connect(state.from, state.port, targetId);
    }
  }
  drag.value = { kind: "none" };
}

function onWheel(event: WheelEvent) {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    zoomBy(event.deltaY > 0 ? 0.92 : 1.08);
    return;
  }
  pan.value = { x: pan.value.x - event.deltaX, y: pan.value.y - event.deltaY };
}

function onDrop(event: DragEvent) {
  const type = event.dataTransfer?.getData("application/x-flow-node");
  if (!type) return;
  const point = toCanvas(event);
  const node = flow.addNode(type, point.x - 60, point.y - 30);
  if (node) {
    selectedNodeId.value = node.id;
    ElMessage.success(`已添加节点 ${node.name}`);
  }
}

// ── commands ──

async function save() {
  if (flow.errors.length) {
    ElMessage.error(`存在 ${flow.errors.length} 个错误，请先修复`);
    return;
  }
  const ok = await flow.save();
  if (ok) ElMessage.success("已保存");
  else ElMessage.error(flow.lastError || "保存失败");
}

async function runFlow() {
  if (!flow.currentId) return;
  if (flow.dirty) {
    const ok = await flow.save();
    if (!ok) {
      ElMessage.error("未保存，无法运行");
      return;
    }
  }
  runningFlow.value = true;
  try {
    const result = await api.run(flow.currentId, { dry_run: dryRun.value });
    showTrace.value = true;
    await trace.attach(result.run_id);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error));
  } finally {
    runningFlow.value = false;
  }
}

async function abort() {
  if (!trace.runId) return;
  await api.abortRun(trace.runId);
}

function setPath(path: LocatePath | null) {
  if (selectedNodeId.value) flow.updateNode(selectedNodeId.value, { path });
}

function removeSelected() {
  if (selectedNodeId.value) {
    flow.removeNode(selectedNodeId.value);
    selectedNodeId.value = null;
  } else if (selectedEdgeId.value) {
    flow.removeEdge(selectedEdgeId.value);
    selectedEdgeId.value = null;
  }
}

function onKey(event: KeyboardEvent) {
  const tag = (event.target as HTMLElement | null)?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    removeSelected();
  } else if (event.key === "0") {
    fit();
  }
}

async function loadTargets() {
  try {
    const result = await api.targets();
    targets.value = result.targets.map((t) => t.name);
  } catch {
    /* the palette still works without the target list */
  }
}

onMounted(async () => {
  window.addEventListener("keydown", onKey);
  window.addEventListener("resize", measure);
  measure();

  // The surface is only measurable once the trace panel has settled, so watch
  // it rather than guessing a frame count.
  if (surface.value) {
    surfaceObserver = new ResizeObserver(() => scheduleFit());
    surfaceObserver.observe(surface.value);
  }

  await flow.loadCatalogue().catch(() => undefined);
  await loadTargets();
  const id = route.params.id as string | undefined;
  if (id) {
    await flow.open(id);
    scheduleFit(true);
  }
});

onUnmounted(() => {
  window.removeEventListener("keydown", onKey);
  window.removeEventListener("resize", measure);
  surfaceObserver?.disconnect();
  surfaceObserver = null;
  trace.detach();
});

watch(
  () => route.params.id,
  async (id) => {
    if (typeof id === "string" && id && id !== flow.currentId) {
      await flow.open(id);
      scheduleFit(true);
    }
  },
);
</script>

<template>
  <div class="canvas-page">
    <header class="canvas-head">
      <div class="head-left">
        <el-button size="small" :icon="Back" text @click="router.push('/flows')">流程</el-button>
        <h1>{{ flow.currentName || "未选择流程" }}</h1>
        <el-tag v-if="flow.dirty" size="small" type="warning" effect="plain">未保存</el-tag>
        <el-tag v-else-if="!flow.loading" size="small" type="success" effect="plain">已保存</el-tag>
      </div>

      <div class="head-right">
        <span class="zoom">{{ Math.round(zoom * 100) }}%</span>
        <el-button size="small" :icon="FullScreen" text title="适应画布 (0)" @click="fit" />
        <el-button size="small" text @click="zoomBy(0.85)">−</el-button>
        <el-button size="small" text @click="zoomBy(1.18)">＋</el-button>

        <el-divider direction="vertical" />

        <el-switch
          v-model="dryRun"
          size="small"
          inline-prompt
          active-text="试运行"
          inactive-text="真实执行"
        />

        <el-button size="small" :icon="Refresh" :loading="flow.saving" :disabled="!flow.dirty" @click="save">
          保存
        </el-button>
        <el-button
          v-if="!runningFlow"
          size="small"
          type="primary"
          :icon="VideoPlay"
          :disabled="!flow.currentId"
          @click="runFlow"
        >
          运行
        </el-button>
        <el-button v-else size="small" type="danger" :icon="VideoPause" @click="abort">中止</el-button>
        <el-button size="small" text @click="showTrace = !showTrace">轨迹</el-button>

        <template v-if="narrow">
          <el-button
            size="small"
            text
            :type="paletteOpen ? 'primary' : undefined"
            @click="paletteOpen = !paletteOpen; inspectorOpen = false"
          >
            节点
          </el-button>
          <el-button
            size="small"
            text
            :type="inspectorOpen ? 'primary' : undefined"
            @click="inspectorOpen = !inspectorOpen; paletteOpen = false"
          >
            参数
          </el-button>
        </template>
      </div>
    </header>

    <!-- Validation is stated before the run, not discovered during it. -->
    <div v-if="flow.errors.length || flow.warnings.length" class="validation-bar" :class="{ error: flow.errors.length }">
      <span v-if="flow.errors.length" class="v-item error">
        <b>{{ flow.errors.length }}</b> 个错误：{{ flow.errors[0]?.message }}
      </span>
      <span v-if="flow.warnings.length" class="v-item warn">
        <b>{{ flow.warnings.length }}</b> 个警告{{ flow.errors.length ? " · " : "：" }}{{ flow.warnings[0]?.message }}
      </span>
      <span
        v-if="flow.undecidedPathNodes.length"
        class="v-item warn"
      >{{ flow.undecidedPathNodes.length }} 个双路径节点尚未选定路径</span>
    </div>

    <div class="canvas-body">
      <NodePalette
        v-show="!narrow || paletteOpen"
        class="drawer"
        :class="{ left: paletteOpen }"
        :categories="flow.catalogue?.categories ?? []"
        @add="(type) => flow.addNode(type, 120, 120)"
      />

      <div
        ref="surface"
        class="surface"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointerleave="onPointerUp"
        @wheel.prevent="onWheel"
        @dragover.prevent
        @drop="onDrop"
      >
        <div class="surface-inner" :style="{ transform }">
          <EdgeLayer
            :edges="flow.draft.edges"
            :nodes="flow.draft.nodes"
            :selected-edge="selectedEdgeId"
            :port-offsets="portOffsets"
            :width="BOUNDS.w"
            :height="BOUNDS.h"
            @select="(id) => { selectedEdgeId = id; selectedNodeId = null; }"
            @remove="(id) => { flow.removeEdge(id); selectedEdgeId = null; }"
          />

          <div
            v-for="node in flow.draft.nodes"
            :key="node.id"
            :data-node-id="node.id"
            class="node-anchor"
            :style="{ left: `${node.position.x}px`, top: `${node.position.y}px` }"
            @pointerdown="startNodeDrag($event, node.id)"
          >
            <NodeCard
              :node="node"
              :spec="flow.specFor(node.type)"
              :selected="selectedNodeId === node.id"
              :effective="flow.effectivePath(node)"
              :issues="flow.issuesByNode(node.id)"
              :running="runningNodeIds.includes(node.id)"
              @select="(id) => { selectedNodeId = id; selectedEdgeId = null; }"
              @remove="(id) => { flow.removeNode(id); if (selectedNodeId === id) selectedNodeId = null; }"
              @set-entry="(id) => flow.setEntry(id)"
              @set-path="(id, p) => flow.updateNode(id, { path: p })"
              @port-down="(id, port, ev) => startLink(id, port, ev)"
            />
          </div>
        </div>

        <p v-if="!flow.draft.nodes.length" class="surface-empty">
          从左侧把节点拖到这里开始设计流程
        </p>

        <!-- Rubber band for the in-progress connection. -->
        <svg v-if="drag.kind === 'link'" class="link-preview">
          <line
            :x1="(flow.nodeById(drag.from)?.position.x ?? 0) + 208"
            :y1="(flow.nodeById(drag.from)?.position.y ?? 0) + 48 + (portOffsets[`${drag.from}:${drag.port}`] ?? 0)"
            :x2="drag.x"
            :y2="drag.y"
            stroke="#6b5bb5"
            stroke-width="1.6"
            stroke-dasharray="4 3"
          />
        </svg>
      </div>

      <!-- Tapping the scrim dismisses a drawer; the canvas stays put underneath. -->
      <div
        v-if="narrow && (paletteOpen || inspectorOpen)"
        class="scrim"
        @pointerdown="paletteOpen = false; inspectorOpen = false"
      />

      <InspectorPanel
        v-show="!narrow || inspectorOpen"
        class="drawer"
        :class="{ right: inspectorOpen }"
        :node="selectedNode"
        :spec="selectedNode ? flow.specFor(selectedNode.type) : undefined"
        :effective="selectedNode ? flow.effectivePath(selectedNode) : null"
        :issues="selectedNode ? flow.issuesByNode(selectedNode.id) : []"
        :incoming="incomingEdges"
        :targets="targets"
        @patch="(patch) => selectedNodeId && flow.updateNode(selectedNodeId, patch)"
        @set-path="setPath"
        @remove="removeSelected"
      />
    </div>

    <TracePanel
      v-if="showTrace"
      :running-node-ids="runningNodeIds"
      @abort="abort"
    />
  </div>
</template>

<style scoped>
.canvas-page {
  display: flex;
  flex-direction: column;
  flex: 1;
  /* Without these two the header's intrinsic width pushes the whole page wider
     than the window, and the flex children below get squeezed instead of
     scrolling — which is what made the palette balloon and the title wrap to
     one character per line. */
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.canvas-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 0 0 auto;
  min-width: 0;
  gap: 12px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--paper);
}

.head-left,
.head-right {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.head-right {
  flex: 0 0 auto;
}

.head-left h1 {
  margin: 0;
  overflow: hidden;
  color: #24322d;
  font-family: var(--font-serif);
  font-size: 17px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zoom {
  color: #8a9790;
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.validation-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding: 6px 18px;
  border-bottom: 1px solid #eae0c8;
  background: #fbf5e4;
  font-size: 11px;
}

.validation-bar.error {
  background: #fbf0e9;
}

.v-item {
  color: #8a6420;
}

.v-item.error {
  color: #a4552f;
}

.v-item b {
  font-size: 12px;
}

.canvas-body {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.surface {
  position: relative;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  background-color: var(--canvas);
  background-image:
    radial-gradient(circle at 1px 1px, #d3ddd6 1px, transparent 0);
  background-size: 22px 22px;
  cursor: default;
}

.surface-inner {
  position: absolute;
  top: 0;
  left: 0;
  width: 6400px;
  height: 4200px;
  transform-origin: 0 0;
}

/* Each node's graph coordinates live on this wrapper, so it must be the
   positioning context for the card's own `position: absolute`. Without it
   every card anchored to the surface origin and all 18 of them stacked on
   one 5x5px point while the SVG edges drew normally — a graph that looked
   connected but had no readable nodes. */
.node-anchor {
  position: absolute;
}

.surface-empty {
  position: absolute;
  top: 45%;
  left: 0;
  right: 0;
  color: #9aa59e;
  font-size: 12.5px;
  text-align: center;
  pointer-events: none;
}

.link-preview {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

/* ---- narrow-window drawers --------------------------------------------
   Under 1020px the palette and inspector stop being flex siblings and
   float over the surface instead. The surface keeps the full width, which
   is the point: a designer squeezed into 112px cannot be worked with. */
.canvas-body {
  position: relative;
}

.drawer {
  z-index: 3;
  box-shadow: 0 0 0 1px var(--line), 0 18px 44px rgba(36, 50, 45, 0.16);
}

.drawer.left {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
}

.drawer.right {
  position: absolute;
  top: 0;
  bottom: 0;
  right: 0;
}

.scrim {
  position: absolute;
  inset: 0;
  z-index: 2;
  background: rgba(36, 50, 45, 0.16);
  cursor: default;
}

@media (max-width: 1020px) {
  /* The toolbar cannot shrink — it is a row of controls, not prose. Wrapping
     it collapsed the title to one glyph per line, so each half scrolls
     sideways instead and the title keeps its own nowrap ellipsis. */
  .canvas-head {
    gap: 8px;
    padding: 8px 10px;
    overflow: hidden;
  }

  .head-left,
  .head-right {
    flex-wrap: nowrap;
    gap: 6px;
  }

  .head-left {
    flex: 0 1 auto;
    overflow: hidden;
  }

  .head-left h1 {
    flex: 0 1 auto;
    min-width: 48px;
  }

  .head-right {
    flex: 1 1 auto;
    /* `safe` is the load-bearing word: plain flex-end overflows toward the
       start, so the zoom controls were pushed off the left edge where the
       scroll container can never reach them. safe falls back to start. */
    justify-content: safe flex-end;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .head-right::-webkit-scrollbar {
    display: none;
  }
}
</style>
