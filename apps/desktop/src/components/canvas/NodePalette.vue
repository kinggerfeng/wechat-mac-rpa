<script setup lang="ts">
// Node palette.
//
// The 双路径 group is deliberately its own section rather than a filter: the two
// strategies are the central design decision, and burying them among 30 node
// types would hide exactly the thing the designer is choosing between.

import { computed, ref } from "vue";
import { Search } from "@element-plus/icons-vue";
import type { NodeCategory, NodeSpec } from "@shared/types";

const props = defineProps<{ categories: NodeCategory[] }>();

const emit = defineEmits<{ (e: "add", type: string, event: DragEvent): void }>();

const query = ref("");

const filtered = computed<NodeCategory[]>(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return props.categories;
  return props.categories
    .map((group) => ({
      category: group.category,
      nodes: group.nodes.filter(
        (spec) =>
          spec.label.toLowerCase().includes(needle) ||
          spec.type.toLowerCase().includes(needle) ||
          spec.doc.toLowerCase().includes(needle),
      ),
    }))
    .filter((group) => group.nodes.length > 0);
});

function onDragStart(spec: NodeSpec, event: DragEvent) {
  event.dataTransfer?.setData("application/x-flow-node", spec.type);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy";
}
</script>

<template>
  <aside class="palette">
    <div class="palette-head">
      <p class="eyebrow">NODE PALETTE</p>
      <el-input
        v-model="query"
        :prefix-icon="Search"
        size="small"
        clearable
        placeholder="搜索节点"
        aria-label="搜索节点"
      />
    </div>

    <div class="palette-body">
      <section v-for="group in filtered" :key="group.category" class="group">
        <header class="group-head" :class="{ dual: group.category === '双路径' }">
          {{ group.category }}
          <span>{{ group.nodes.length }}</span>
        </header>
        <div
          v-for="spec in group.nodes"
          :key="spec.type"
          class="entry"
          :class="{ aware: spec.path_aware }"
          draggable="true"
          :title="spec.doc"
          @dragstart="onDragStart(spec, $event)"
          @dblclick="emit('add', spec.type, $event as DragEvent)"
        >
          <span class="entry-label">{{ spec.label }}</span>
          <span v-if="spec.path_aware" class="entry-tag">双路径</span>
        </div>
      </section>

      <p v-if="!filtered.length" class="empty">没有匹配的节点</p>
    </div>
  </aside>
</template>

<style scoped>
.palette {
  width: 216px;
  flex: 0 0 216px;
  min-width: 216px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--line);
  background: #fbfcfa;
}

.palette-head {
  padding: 13px 12px 9px;
  border-bottom: 1px solid var(--line);
}

.eyebrow {
  margin: 0 0 8px;
  color: #809087;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1.3px;
}

.palette-body {
  flex: 1;
  overflow-y: auto;
  padding: 6px 0 20px;
}

.group {
  margin-bottom: 6px;
}

.group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px 4px;
  color: #8a9790;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1px;
}

.group-head.dual {
  color: var(--path-vision);
}

.group-head span {
  font-weight: 500;
  opacity: 0.7;
}

.entry {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin: 1px 6px;
  padding: 6px 9px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  color: #35443e;
  font-size: 11.5px;
  cursor: grab;
  transition: background 0.1s, border-color 0.1s;
}

.entry:hover {
  border-color: #cfdcd4;
  background: #f0f5f1;
}

.entry:active {
  cursor: grabbing;
}

.entry.aware {
  background: linear-gradient(90deg, rgba(107, 91, 181, 0.05), transparent 60%);
}

.entry-tag {
  flex: 0 0 auto;
  padding: 1px 5px;
  border-radius: 2px;
  background: var(--path-vision-soft);
  color: var(--path-vision);
  font-size: 8.5px;
  font-weight: 600;
  letter-spacing: 0.3px;
}

.empty {
  padding: 20px 12px;
  color: #9aa59e;
  font-size: 11px;
  text-align: center;
}
</style>
