<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Aim,
  AlarmClock,
  Clock,
  Document,
  Lock,
  Odometer,
  Share,
  VideoPause,
  VideoPlay,
} from "@element-plus/icons-vue";

import { useEngineStore } from "./stores/engine";
import { useFlowStore } from "./stores/flow";

const engine = useEngineStore();
const flow = useFlowStore();
const route = useRoute();
const router = useRouter();
const navOpen = ref(false);

interface NavItem {
  name: string;
  title: string;
  icon: unknown;
}

const navItems: NavItem[] = [
  { name: "overview", title: "运行总览", icon: Odometer },
  { name: "flows", title: "流程", icon: Share },
  { name: "runs", title: "运行记录", icon: Clock },
  { name: "elements", title: "元素库", icon: Aim },
  { name: "schedules", title: "计划任务", icon: AlarmClock },
  { name: "logs", title: "日志", icon: Document },
  { name: "permissions", title: "权限与设置", icon: Lock },
];

const visibleNav = computed(() => navItems.filter((item) => item.name !== "canvas"));
const activeName = computed(() => (route.name === "canvas" ? "flows" : String(route.name ?? "")));
const currentTitle = computed(() => (route.meta?.title as string | undefined) ?? "控制台");

const missingPermissions = computed(
  () => engine.permissions?.missing.filter((key) => key !== "automation") ?? [],
);

async function toggleBot() {
  try {
    if (engine.running) await engine.stopBot();
    else await engine.startBot();
  } catch {
    // The store already surfaced the reason — most often a 409 with the list of
    // missing grants, which the permission page explains in full.
  }
}

onMounted(() => {
  engine.startPolling();
  void flow.loadCatalogue().catch(() => undefined);
});

onUnmounted(() => engine.stopPolling());
</script>

<template>
  <div class="shell">
    <aside class="rail" :class="{ open: navOpen }">
      <div class="brand-mark"><span>W</span></div>
      <div class="rail-rule" />
      <router-link
        v-for="item in visibleNav"
        :key="item.name"
        class="rail-button"
        :class="{ selected: activeName === item.name }"
        :to="{ name: item.name }"
        :title="item.title"
        @click="navOpen = false"
      >
        <el-icon><component :is="item.icon" /></el-icon>
      </router-link>
      <div class="rail-bottom">RPA<br /><b>DESK</b></div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="breadcrumbs">
          <span>WECHAT MAC RPA</span><i>/</i><b>{{ currentTitle }}</b>
        </div>
        <div class="top-meta">
          <button
            v-if="missingPermissions.length"
            class="perm-warning"
            :title="`缺少权限：${missingPermissions.join('、')}`"
            @click="router.push('/permissions')"
          >
            缺少 {{ missingPermissions.length }} 项权限
          </button>
          <span class="api-state" :class="{ online: engine.online }">
            <span />{{ engine.online ? "LOCAL API 已连接" : "LOCAL API 离线" }}
          </span>
          <el-button
            v-if="!route.meta?.hidden"
            :type="engine.running ? 'danger' : 'primary'"
            :plain="engine.running"
            :icon="engine.running ? VideoPause : VideoPlay"
            :loading="engine.busy"
            :disabled="!engine.online"
            size="small"
            @click="toggleBot"
          >
            {{ engine.running ? "停止 Bot" : "启动 Bot" }}
          </el-button>
        </div>
      </header>

      <el-alert
        v-if="engine.lastError"
        class="global-error"
        :title="engine.lastError"
        type="error"
        show-icon
        :closable="false"
      />

      <router-view v-slot="{ Component }">
        <component :is="Component" />
      </router-view>
    </section>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100vh;
}

.rail {
  width: 76px;
  flex: 0 0 76px;
  display: flex;
  flex-direction: column;
  align-items: center;
  background: var(--rail);
  color: #e6eee8;
  padding: 22px 0 18px;
}

.brand-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 1px solid #729282;
  color: #fff;
  font-family: Georgia, serif;
  font-size: 20px;
  font-style: italic;
}

.rail-rule {
  width: 28px;
  height: 1px;
  margin: 22px 0 12px;
  background: #46665a;
}

.rail-button {
  width: 42px;
  height: 42px;
  margin: 3px 0;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius);
  color: #99b0a3;
  background: transparent;
  text-decoration: none;
  cursor: pointer;
  transition: background 0.14s, color 0.14s;
}

.rail-button .el-icon {
  font-size: 18px;
}

.rail-button:hover {
  color: #fff;
  background: var(--rail-active);
}

.rail-button.selected {
  color: #fff;
  background: var(--rail-active);
}

.rail-bottom {
  margin-top: auto;
  color: #87a194;
  font-size: 9px;
  line-height: 1.5;
  letter-spacing: 1px;
  text-align: center;
}

.rail-bottom b {
  color: #e2eee6;
  font-weight: 600;
}

.workspace {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
}

.topbar {
  height: 60px;
  flex: 0 0 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 28px;
  border-bottom: 1px solid var(--line);
  background: rgba(248, 250, 247, 0.9);
}

.breadcrumbs {
  display: flex;
  align-items: center;
  gap: 12px;
  color: #8a9691;
  font-size: 10px;
  letter-spacing: 1px;
}

.breadcrumbs i {
  color: #bdc7c1;
  font-style: normal;
}

.breadcrumbs b {
  color: #33443f;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0;
}

.top-meta {
  display: flex;
  align-items: center;
  gap: 14px;
}

.perm-warning {
  border: 1px solid #e6c9a8;
  border-radius: var(--radius);
  background: #fdf3e7;
  color: #9a6551;
  padding: 4px 9px;
  font-size: 11px;
  cursor: pointer;
}

.perm-warning:hover {
  background: #fbe9d6;
}

.api-state {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #9a6551;
  font-size: 10px;
}

.api-state > span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--orange);
}

.api-state.online {
  color: var(--green);
}

.api-state.online > span {
  background: #52a27c;
}

.global-error {
  margin: 14px 28px 0;
  width: auto;
}

@media (max-width: 720px) {
  .rail {
    width: 56px;
    flex-basis: 56px;
  }

  .topbar {
    padding: 0 14px;
  }

  .perm-warning {
    display: none;
  }
}
</style>
