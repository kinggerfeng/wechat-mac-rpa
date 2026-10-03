<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  Collection,
  DocumentChecked,
  List,
  Notebook,
  Picture,
  ScaleToOriginal,
  Switch,
  TrendCharts,
} from "@element-plus/icons-vue";

import { useConnectionStore } from "./stores/connection";

/**
 * The operations console shell.
 *
 * Deliberately not a copy of the desktop shell with a shorter nav. The
 * differences are the point:
 *
 * - no 启动 Bot button and no permission warnings. An operator reviews what
 *   the bot did; they never drive the machine, so screen recording and
 *   accessibility are not theirs to grant and a red badge would be a lie about
 *   whose problem it is.
 * - no canvas, no flows, no element picker. Those are the end user's work.
 */
const connection = useConnectionStore();
const route = useRoute();
const navOpen = ref(false);

interface NavItem {
  name: string;
  title: string;
  icon: unknown;
}

const nav: NavItem[] = [
  { name: "ticks", title: "Tick 记录", icon: List },
  { name: "ground-truth", title: "真值对比", icon: ScaleToOriginal },
  { name: "reviews", title: "案例库", icon: Collection },
  { name: "screenshots", title: "截图", icon: Picture },
  { name: "benchmarks", title: "Benchmark", icon: TrendCharts },
  { name: "experiments", title: "实验 A/B", icon: Switch },
  { name: "code-audit", title: "代码审计", icon: DocumentChecked },
  { name: "wiki-review", title: "Wiki 审核", icon: Notebook },
];

/** A detail route highlights its list parent, so the rail is never fully
 * unselected while you are two levels deep. */
const activeName = computed(() => {
  const current = String(route.name ?? "");
  const parent = route.meta?.parent as string | undefined;
  return parent ?? current;
});


/**
 * Three states, not two. "未连接" said as "已连接" before the first poll lands
 * is a reading nobody made.
 */
const connectionLabel = computed(() => {
  if (connection.online === null) return "正在连接…";
  return connection.online ? "LOCAL API 已连接" : "LOCAL API 未连接";
});

let timer: number | null = null;

onMounted(() => {
  void connection.refresh();
  timer = window.setInterval(() => void connection.refresh(), 5000);
});

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
});
</script>

<template>
  <div class="shell">
    <aside class="rail" :class="{ open: navOpen }">
      <div class="brand-mark"><span>W</span></div>
      <div class="rail-rule" />
      <nav class="rail-nav">
        <router-link
          v-for="item in nav"
          :key="item.name"
          class="rail-button"
          :class="{ selected: activeName === item.name }"
          :to="{ name: item.name }"
          :title="item.title"
          @click="navOpen = false"
        >
          <el-icon><component :is="item.icon" /></el-icon>
        </router-link>
      </nav>
      <div class="rail-bottom">OPS<br /><b>CONSOLE</b></div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="breadcrumbs">
          <span>WECHAT MAC RPA</span><i>/</i><b>运营平台</b>
        </div>
        <div class="top-meta">
          <span class="conn" :class="{ off: connection.online === false }">
            <i class="dot" />{{ connectionLabel }}
          </span>
        </div>
      </header>

      <main class="canvas-scroll">
        <router-view />
      </main>
    </section>
  </div>
</template>

<style scoped>
.shell { display: flex; height: 100vh; background: var(--canvas); color: var(--ink); }

.rail {
  width: 76px; flex: 0 0 76px; background: var(--green-deep);
  display: flex; flex-direction: column; align-items: center; padding: 16px 0;
}
.brand-mark {
  width: 44px; height: 44px; display: grid; place-items: center;
  border: 1px solid var(--green-line); color: var(--paper);
  font-family: var(--font-display); font-size: 22px;
}
.rail-rule { width: 28px; border-top: 1px solid var(--green-line); margin: 14px 0 18px; }
.rail-nav { display: flex; flex-direction: column; gap: 8px; }
.rail-button {
  width: 42px; height: 42px; display: grid; place-items: center;
  color: var(--green-muted); border-radius: 6px;
}
.rail-button:hover { background: rgba(255, 255, 255, 0.08); color: var(--paper); }
.rail-button.selected { background: var(--green-mid); color: var(--paper); }
.rail-bottom {
  margin-top: auto; color: var(--green-muted); font-size: 10px;
  letter-spacing: 0.14em; text-align: center; line-height: 1.5;
}
.rail-bottom b { color: var(--paper); }

.workspace { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
  height: 60px; flex: 0 0 60px; display: flex; align-items: center;
  justify-content: space-between; padding: 0 24px;
  border-bottom: 1px solid var(--line); background: var(--paper);
}
.breadcrumbs { font-size: 14px; color: var(--ink-muted); letter-spacing: 0.04em; }
.breadcrumbs i { margin: 0 10px; font-style: normal; opacity: 0.5; }
.breadcrumbs b { color: var(--ink); font-weight: 600; }
.top-meta { display: flex; align-items: center; gap: 16px; }
.conn { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: var(--green-deep); }
.conn.off { color: var(--orange); }
.conn .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); }
.conn.off .dot { background: var(--orange); }

.canvas-scroll { flex: 1; overflow: auto; }
</style>
