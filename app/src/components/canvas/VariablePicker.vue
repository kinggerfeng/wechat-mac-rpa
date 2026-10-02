<script setup lang="ts">
// Text field for a `variable` parameter, with the values that are actually in
// scope one click away.
//
// It stays a free-text field on purpose. A dropdown alone would be wrong: a
// variable can legitimately be produced by a sub-flow, a loop the author has
// not drawn yet, or a run-time value with no declaration anywhere — and an
// editor that refuses to let you type one makes those flows unauthorable. The
// list is a shortcut, never a constraint.

import { computed, nextTick, ref } from "vue";
import type { VariableRef } from "../../composables/useVariableCatalog";

const props = defineProps<{
  modelValue: string;
  placeholder?: string;
  refs: VariableRef[];
  ambiguous: Set<string>;
}>();

const emit = defineEmits<{ (e: "update:modelValue", value: string): void }>();

const open = ref(false);
const input = ref<{ $el?: HTMLElement; focus: () => void } | null>(null);

const groups = computed(() => {
  const order: VariableRef["group"][] = ["流程变量", "上游输出"];
  return order
    .map((group) => ({ group, items: props.refs.filter((r) => r.group === group) }))
    .filter((entry) => entry.items.length > 0);
});

/**
 * Insert `{{ref}}` at the caret rather than replacing the field.
 *
 * The value is a sentence more often than not — `"给 {{name}} 回一条消息"` — and
 * overwriting it would make the picker useless for exactly the strings it is
 * meant to help write. Falls back to appending when the underlying input cannot
 * be reached (Element Plus internals moved), because losing the caret position
 * must not lose the insertion.
 */
async function insert(ref: VariableRef): Promise<void> {
  const token = `{{${ref.ref}}}`;
  const current = props.modelValue ?? "";
  const el = input.value?.$el?.querySelector("input") as HTMLInputElement | null;
  if (!el) {
    emit("update:modelValue", current ? `${current}${token}` : token);
    open.value = false;
    return;
  }
  const start = el.selectionStart ?? current.length;
  const end = el.selectionEnd ?? start;
  emit("update:modelValue", `${current.slice(0, start)}${token}${current.slice(end)}`);
  open.value = false;
  await nextTick();
  const caret = start + token.length;
  el.focus();
  el.setSelectionRange(caret, caret);
}

const caret = ref<number | null>(null);

function rememberCaret(): void {
  const el = input.value?.$el?.querySelector("input") as HTMLInputElement | null;
  if (el) caret.value = el.selectionStart;
}
</script>

<template>
  <div class="var-picker">
    <el-input
      ref="input"
      :model-value="modelValue"
      size="small"
      :placeholder="placeholder"
      @update:model-value="emit('update:modelValue', $event)"
      @select="rememberCaret"
      @click="rememberCaret"
      @keyup="rememberCaret"
    >
      <template #suffix>
        <el-popover
          v-model:visible="open"
          placement="bottom-end"
          :width="320"
          trigger="click"
          popper-class="var-popover"
        >
          <template #reference>
            <button class="var-btn" type="button" title="插入变量">{'{{ }}'}</button>
          </template>

          <div v-if="groups.length" class="var-menu">
            <div v-for="entry in groups" :key="entry.group" class="var-group">
              <p class="var-group-title">{{ entry.group }}</p>
              <button
                v-for="item in entry.items"
                :key="`${item.group}-${item.ref}`"
                class="var-item"
                :class="{ warn: item.ambiguous }"
                type="button"
                @click="insert(item)"
              >
                <span class="var-name">{{ item.name }}</span>
                <span v-if="item.from" class="var-from">{{ item.from }}</span>
                <span v-if="item.ambiguous" class="var-flag">重名</span>
              </button>
            </div>
          </div>
          <p v-else class="var-empty">
            上游还没有产出任何变量。流程变量可以在画布左上角的「流程变量」里声明。
          </p>
        </el-popover>
      </template>
    </el-input>
  </div>
</template>

<style scoped>
.var-btn {
  border: 0;
  background: transparent;
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 1;
  padding: 2px 3px;
  border-radius: 2px;
  color: var(--muted);
}
.var-btn:hover {
  background: var(--path-element-soft);
  color: var(--green-dark);
}
.var-menu {
  max-height: 320px;
  overflow: auto;
}
.var-group + .var-group {
  margin-top: 10px;
}
.var-group-title {
  margin: 0 0 4px;
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--muted);
}
.var-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  width: 100%;
  border: 0;
  background: transparent;
  text-align: left;
  cursor: pointer;
  padding: 3px 4px;
  border-radius: 2px;
  font-size: 11px;
}
.var-item:hover {
  background: var(--path-element-soft);
}
.var-name {
  font-family: var(--font-mono);
  color: var(--ink);
}
.var-from {
  color: var(--muted);
  font-size: 10px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.var-flag {
  color: var(--orange);
  font-size: 9px;
  border: 1px solid var(--orange);
  border-radius: 2px;
  padding: 0 3px;
  flex: none;
}
.var-empty {
  margin: 0;
  font-size: 11px;
  color: var(--muted);
  line-height: 1.6;
}
</style>
