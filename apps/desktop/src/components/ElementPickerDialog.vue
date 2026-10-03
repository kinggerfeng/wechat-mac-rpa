<script setup lang="ts">
// Element picker: screenshot, drag a rectangle, store it as an element.
//
// The three-way outcome (OCR anchor / image template / fixed rect) is surfaced
// rather than hidden, because it changes how long the element keeps working: an
// OCR-anchored element survives the window moving, a fixed rect stops the
// moment anything shifts. Saying "saved!" and hiding that difference is how an
// automation quietly breaks a week later.

import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { Element, PickScreenshot } from "@shared/types";

const props = defineProps<{ modelValue: boolean; flowId?: string | null }>();
const emit = defineEmits<{
  "update:modelValue": [boolean];
  picked: [Element];
}>();

const shot = ref<PickScreenshot | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const name = ref("");
/** The dragged rectangle, in the screenshot's own pixel space. */
const box = ref({ x: 0, y: 0, w: 0, h: 0 });
const dragging = ref(false);
const origin = ref({ x: 0, y: 0 });
const surface = ref<HTMLElement | null>(null);

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit("update:modelValue", value),
});

/** The screenshot is Retina; CSS pixels are half of that. */
const cssScale = computed(() => (shot.value?.scale_factor || 2) / 2);
const hasBox = computed(() => box.value.w > 3 && box.value.h > 3);

const boxStyle = computed(() => ({
  left: `${box.value.x * cssScale.value}px`,
  top: `${box.value.y * cssScale.value}px`,
  width: `${box.value.w * cssScale.value}px`,
  height: `${box.value.h * cssScale.value}px`,
}));

async function load() {
  loading.value = true;
  error.value = "";
  box.value = { x: 0, y: 0, w: 0, h: 0 };
  try {
    shot.value = await api.pickScreenshot();
    name.value = "";
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : String(exc);
    shot.value = null;
  } finally {
    loading.value = false;
  }
}

function pointAt(event: MouseEvent) {
  const rect = surface.value?.getBoundingClientRect();
  const scale = cssScale.value || 1;
  return {
    x: Math.max(0, Math.round((event.clientX - (rect?.left ?? 0)) / scale)),
    y: Math.max(0, Math.round((event.clientY - (rect?.top ?? 0)) / scale)),
  };
}

function onDown(event: MouseEvent) {
  if (!shot.value) return;
  origin.value = pointAt(event);
  dragging.value = true;
}

function onMove(event: MouseEvent) {
  if (!dragging.value) return;
  const point = pointAt(event);
  box.value = {
    x: Math.min(origin.value.x, point.x),
    y: Math.min(origin.value.y, point.y),
    w: Math.abs(point.x - origin.value.x),
    h: Math.abs(point.y - origin.value.y),
  };
}

function onUp() {
  dragging.value = false;
}

async function save() {
  if (!shot.value || !hasBox.value) return;
  saving.value = true;
  error.value = "";
  try {
    const result = await api.pick({
      name: name.value.trim() || `元素_${Date.now()}`,
      image_path: shot.value.image_path,
      x: box.value.x,
      y: box.value.y,
      width: box.value.w,
      height: box.value.h,
      flow_id: props.flowId ?? null,
    });
    ElMessage.success(result.advice || "已保存元素");
    emit("picked", result.element);
    open.value = false;
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : String(exc);
  } finally {
    saving.value = false;
  }
}

watch(open, (value) => {
  if (value) void load();
});

onBeforeUnmount(() => {
  dragging.value = false;
});
</script>

<template>
  <el-dialog v-model="open" title="拾取元素" width="880px" append-to-body>
    <div class="picker">
      <p class="hint">
        在截图上拖出一个矩形。区域内有文字时会用文字做锚点，窗口移动后仍能定位；
        没有文字则截取模板图；两者都没有时按固定坐标保存。
      </p>

      <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon class="err" />

      <div v-loading="loading" class="stage">
        <div
          v-if="shot"
          ref="surface"
          class="canvas"
          @mousedown="onDown"
          @mousemove="onMove"
          @mouseup="onUp"
          @mouseleave="onUp"
        >
          <img :src="`file://${shot.image_path}`" alt="窗口截图" draggable="false" />
          <div v-if="hasBox" class="box" :style="boxStyle">
            <span class="box-size">{{ box.w }}×{{ box.h }}</span>
          </div>
        </div>
        <el-empty v-else-if="!loading" description="无法获取截图" />
      </div>

      <el-form label-width="72px" class="form">
        <el-form-item label="元素名称">
          <el-input v-model="name" placeholder="例如：搜索框" maxlength="80" />
        </el-form-item>
      </el-form>
    </div>

    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button :icon="Refresh" @click="load">重新截图</el-button>
      <el-button type="primary" :loading="saving" :disabled="!hasBox" @click="save">
        保存元素
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hint {
  margin: 0;
  color: #6f7d76;
  font-size: 12px;
  line-height: 1.6;
}

.err {
  margin: 0;
}

.stage {
  min-height: 320px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: #f4f7f4;
  overflow: auto;
}

.canvas {
  position: relative;
  line-height: 0;
  cursor: crosshair;
  user-select: none;
}

.canvas img {
  display: block;
  max-width: 100%;
}

.box {
  position: absolute;
  border: 1.5px solid #357b63;
  background: rgba(53, 123, 99, 0.14);
  pointer-events: none;
}

.box-size {
  position: absolute;
  top: -19px;
  left: 0;
  padding: 0 5px;
  border-radius: 3px;
  background: #357b63;
  color: #fff;
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 15px;
  white-space: nowrap;
}

.form {
  margin: 0;
}
</style>
