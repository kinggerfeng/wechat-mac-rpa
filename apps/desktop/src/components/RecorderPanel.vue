<script setup lang="ts">
// Recorder panel: start capturing, review what was captured, commit it.
//
// The panel deliberately separates "captured" from "committed". A recording is
// raw input events with coordinates, and the warnings it shows are about that:
// most captured clicks are literal coordinates that will break when the window
// moves. Committing is one click, but the author is told what they are getting.

import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { VideoPause, VideoPlay } from "@element-plus/icons-vue";

import { api, ApiError } from "@shared/api/client";
import type { RecordedAction, RecordStatus, RecordStopResult } from "@shared/types";

const emit = defineEmits<{ committed: [string] }>();

const status = ref<RecordStatus>({ recording: false, error: "", count: 0 });
const captured = ref<RecordStopResult | null>(null);
const busy = ref(false);
const committing = ref(false);
const flowName = ref("录制流程");
let timer: number | null = null;

const rows = computed<RecordedAction[]>(() => captured.value?.actions ?? []);

/** A recording with literal coordinates is a draft, and says so. */
const warnings = computed<string[]>(() => captured.value?.warnings ?? []);

async function poll() {
  try {
    status.value = await api.recordStatus();
  } catch {
    status.value = { recording: false, error: "无法连接本地服务", count: 0 };
  }
}

async function start() {
  busy.value = true;
  captured.value = null;
  try {
    await api.recordStart();
    await poll();
    ElMessage.success("开始录制，操作软件后点「停止」");
  } catch (exc) {
    const message = exc instanceof ApiError ? exc.message : String(exc);
    // A missing Accessibility permission is the common case and has a concrete
    // fix, so it is shown as an error rather than as "start failed".
    ElMessage.error({ message, duration: 6000 });
    await poll();
  } finally {
    busy.value = false;
  }
}

async function stop() {
  busy.value = true;
  try {
    captured.value = await api.recordStop();
    await poll();
    if (!rows.value.length) {
      ElMessage.warning("没有捕获到操作。若刚才确实点过，请检查「辅助功能」权限");
    }
  } catch (exc) {
    ElMessage.error(exc instanceof ApiError ? exc.message : String(exc));
  } finally {
    busy.value = false;
  }
}

async function commit() {
  if (!flowName.value.trim()) {
    ElMessage.warning("请填写流程名称");
    return;
  }
  committing.value = true;
  try {
    const result = await api.recordCommit({ name: flowName.value.trim() });
    ElMessage.success(`已创建流程「${result.name}」，共 ${result.action_count} 个动作`);
    emit("committed", result.flow_id);
    captured.value = null;
  } catch (exc) {
    ElMessage.error(exc instanceof ApiError ? exc.message : String(exc));
  } finally {
    committing.value = false;
  }
}

async function discard() {
  await ElMessageBox.confirm("放弃这次录制？已捕获的动作会被清空。", "放弃录制", {
    type: "warning",
    confirmButtonText: "放弃",
    cancelButtonText: "继续录制",
  });
  await api.recordDiscard();
  captured.value = null;
  await poll();
}

function kindLabel(kind: RecordedAction["kind"]): string {
  return {
    click: "单击",
    double_click: "双击",
    scroll: "滚动",
    type_keys: "输入",
    hotkey: "按键",
  }[kind];
}

function kindTag(kind: RecordedAction["kind"]): string {
  return kind === "type_keys" || kind === "hotkey" ? "info" : "warning";
}

onMounted(() => {
  void poll();
  timer = window.setInterval(() => void poll(), 1500);
});

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer);
});
</script>

<template>
  <div class="recorder">
    <header class="head">
      <div class="state">
        <span class="dot" :class="{ on: status.recording }" />
        <span>{{ status.recording ? "正在录制" : "未在录制" }}</span>
        <span v-if="status.count" class="count">{{ status.count }} 个动作</span>
      </div>
      <div class="actions">
        <el-button
          v-if="!status.recording"
          type="primary"
          size="small"
          :icon="VideoPlay"
          :loading="busy"
          @click="start"
        >
          开始录制
        </el-button>
        <el-button
          v-else
          type="danger"
          size="small"
          :icon="VideoPause"
          :loading="busy"
          @click="stop"
        >
          停止
        </el-button>
        <el-button v-if="status.recording" size="small" @click="discard">放弃</el-button>
      </div>
    </header>

    <el-alert
      v-if="status.error"
      type="error"
      :closable="false"
      show-icon
      :title="status.error"
      class="alert"
    />

    <div v-if="!status.recording && !rows.length" class="empty">
      <p>录制会捕获你的点击、滚动和键盘输入，并把它们变成可编辑的流程节点。</p>
      <p class="muted">
        捕获到的点击是固定坐标，窗口移动后会失效。提交后可在设计器中把坐标改成引用元素库。
      </p>
    </div>

    <template v-if="rows.length">
      <el-alert
        v-for="(warning, index) in warnings"
        :key="index"
        type="warning"
        :closable="false"
        show-icon
        :title="warning"
        class="alert"
      />

      <el-table :data="rows" size="small" max-height="320" class="table">
        <el-table-column label="#" width="42" align="right">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column label="类型" width="76">
          <template #default="{ row }">
            <el-tag size="small" :type="kindTag(row.kind)" effect="plain">
              {{ kindLabel(row.kind) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="230">
          <template #default="{ row }">{{ row.label }}</template>
        </el-table-column>
        <el-table-column label="应用" min-width="130">
          <template #default="{ row }">
            <span class="muted">{{ row.app || "—" }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="commit">
        <el-input v-model="flowName" size="small" placeholder="流程名称" class="name" />
        <el-button
          type="primary"
          size="small"
          :loading="committing"
          :disabled="!flowName.trim()"
          @click="commit"
        >
          生成流程
        </el-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.recorder {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.state {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #33443f;
  font-size: 12.5px;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #b9c4be;
}

.dot.on {
  background: #d0503c;
  animation: blink 1.1s ease-in-out infinite;
}

@keyframes blink {
  50% {
    opacity: 0.35;
  }
}

.count {
  color: #7c8a83;
  font-family: var(--font-mono);
  font-size: 11px;
}

.actions {
  display: flex;
  gap: 6px;
}

.alert {
  margin: 0;
}

.empty p {
  margin: 0 0 6px;
  color: #5d6b64;
  font-size: 12.5px;
  line-height: 1.7;
}

.muted {
  color: #8a9791 !important;
  font-size: 11.5px;
}

.table {
  width: 100%;
}

.commit {
  display: flex;
  gap: 8px;
  align-items: center;
}

.name {
  max-width: 260px;
}
</style>
