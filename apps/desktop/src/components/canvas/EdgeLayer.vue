<script setup lang="ts">
// SVG edge layer, positioned under the nodes.
//
// Edges are drawn from the node's output stub to the target's left edge, with
// the port used to pick the vertical offset so error/manual branches do not
// overlap the normal path. Clicking selects; the label carries the branch name
// so a designer can tell "ok" from "error" without hovering.

import { computed } from "vue";
import type { FlowEdge, FlowNode } from "@shared/types";

const props = defineProps<{
  edges: FlowEdge[];
  nodes: FlowNode[];
  selectedEdge: string | null;
  portOffsets: Record<string, number>;
  width: number;
  height: number;
}>();

const emit = defineEmits<{
  (e: "select", edgeId: string): void;
  (e: "remove", edgeId: string): void;
}>();

const NODE_W = 208;
const NODE_H = 96;

interface Geometry {
  d: string;
  label: string;
  port: string;
  midX: number;
  midY: number;
  id: string;
}

const geometries = computed<Geometry[]>(() => {
  const byId = new Map(props.nodes.map((n) => [n.id, n]));
  const out: Geometry[] = [];
  for (const edge of props.edges) {
    const from = byId.get(edge.source);
    const to = byId.get(edge.target);
    if (!from || !to) continue;

    const offset = props.portOffsets[`${edge.source}:${edge.source_port}`] ?? 0;
    const x1 = from.position.x + NODE_W;
    const y1 = from.position.y + NODE_H / 2 + offset;
    const x2 = to.position.x;
    const y2 = to.position.y + NODE_H / 2;
    // A horizontal cubic keeps the S-curve readable; back-edges (target left of
    // source) need a wider control offset or they collapse onto the node.
    const span = Math.max(48, Math.abs(x2 - x1) * 0.45);
    const c1 = x1 + span;
    const c2 = x2 - span;
    out.push({
      id: edge.id,
      port: edge.source_port,
      label: edge.condition ? `${edge.source_port} · ${edge.condition}` : edge.source_port,
      d: `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`,
      midX: (x1 + c2 + c2 + x2) / 4,
      midY: (y1 + y1 + y2 + y2) / 4,
    });
  }
  return out;
});

function strokeFor(port: string): string {
  if (port === "ok") return "#7fa897";
  if (port === "error") return "#d98b6a";
  if (port === "manual" || port === "blocked") return "#b5643c";
  if (port === "vision" || port === "true") return "#6b5bb5";
  if (port === "auto") return "#b07d2b";
  return "#a9b5ae";
}
</script>

<template>
  <svg class="edges" :width="width" :height="height">
    <g
      v-for="geo in geometries"
      :key="geo.id"
      class="edge"
      :class="{ selected: selectedEdge === geo.id }"
      @click.stop="emit('select', geo.id)"
    >
      <path class="edge-hit" :d="geo.d" />
      <path :d="geo.d" :stroke="strokeFor(geo.port)" fill="none" stroke-width="1.6" />
      <g :transform="`translate(${geo.midX}, ${geo.midY})`" class="edge-label">
        <rect x="-30" y="-9" width="60" height="17" rx="2" fill="#fbfcfa" stroke="#dce3de" />
        <text x="0" y="3" text-anchor="middle" :fill="strokeFor(geo.port)">{{ geo.label }}</text>
      </g>
      <circle
        v-if="selectedEdge === geo.id"
        :cx="geo.midX"
        :cy="geo.midY + 16"
        r="7"
        class="edge-delete"
        @click.stop="emit('remove', geo.id)"
      />
      <text
        v-if="selectedEdge === geo.id"
        :x="geo.midX"
        :y="geo.midY + 20"
        text-anchor="middle"
        class="edge-delete-glyph"
      >×</text>
    </g>
  </svg>
</template>

<style scoped>
.edges {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: visible;
}

.edge {
  pointer-events: auto;
  cursor: pointer;
}

.edge-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 12;
}

.edge-label text {
  font-family: var(--font-mono);
  font-size: 8.5px;
}

.edge.selected path:not(.edge-hit) {
  stroke-width: 2.4;
}

.edge-delete {
  fill: #f6e3da;
  stroke: #d98b6a;
  cursor: pointer;
}

.edge-delete-glyph {
  font-size: 10px;
  fill: #a4552f;
  pointer-events: none;
}
</style>
