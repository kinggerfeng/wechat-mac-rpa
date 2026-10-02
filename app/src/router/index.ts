import { createRouter, createWebHashHistory } from "vue-router";

// Hash history: the Tauri bundle is served from the filesystem, where a
// path-based history would 404 on any reload that was not the entry URL.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    {
      path: "/overview",
      name: "overview",
      component: () => import("../pages/OverviewPage.vue"),
      meta: { title: "运行总览", icon: "Odometer" },
    },
    {
      path: "/flows",
      name: "flows",
      component: () => import("../pages/FlowListPage.vue"),
      meta: { title: "流程", icon: "Share" },
    },
    {
      path: "/canvas/:id?",
      name: "canvas",
      component: () => import("../pages/CanvasPage.vue"),
      meta: { title: "设计器", icon: "Connection", hidden: true },
    },
    {
      path: "/runs",
      name: "runs",
      component: () => import("../pages/RunHistoryPage.vue"),
      meta: { title: "运行记录", icon: "Clock" },
    },
    {
      path: "/elements",
      name: "elements",
      component: () => import("../pages/ElementPage.vue"),
      meta: { title: "元素库", icon: "Aim" },
    },
    {
      path: "/schedules",
      name: "schedules",
      component: () => import("../pages/SchedulePage.vue"),
      meta: { title: "计划任务", icon: "AlarmClock" },
    },
    {
      path: "/logs",
      name: "logs",
      component: () => import("../pages/LogPage.vue"),
      meta: { title: "日志", icon: "Document" },
    },
    {
      path: "/providers",
      name: "providers",
      component: () => import("../pages/ProviderPage.vue"),
      meta: { title: "大模型网关", icon: "Cpu" },
    },
    {
      path: "/permissions",
      name: "permissions",
      component: () => import("../pages/PermissionPage.vue"),
      meta: { title: "权限与设置", icon: "Lock" },
    },

    // ── cases domain ──
    // Grouped after the engine pages: these read `data/cases.db` and are about
    // the bot's past behaviour, not about driving the machine.
    {
      path: "/ticks",
      name: "ticks",
      component: () => import("../pages/TickListPage.vue"),
      meta: { title: "Tick 记录", icon: "List", group: "cases" },
    },
    {
      path: "/ticks/:id",
      name: "tick-detail",
      component: () => import("../pages/TickDetailPage.vue"),
      meta: { title: "Tick 详情", hidden: true, group: "cases", parent: "ticks" },
    },
    {
      path: "/ground-truth",
      name: "ground-truth",
      component: () => import("../pages/GroundTruthPage.vue"),
      meta: { title: "真值对比", icon: "ScaleToOriginal", group: "cases" },
    },
    {
      path: "/reviews",
      name: "reviews",
      component: () => import("../pages/ReviewListPage.vue"),
      meta: { title: "案例库", icon: "Collection", group: "cases" },
    },
    {
      path: "/screenshots",
      name: "screenshots",
      component: () => import("../pages/ScreenshotListPage.vue"),
      meta: { title: "截图", icon: "Picture", group: "cases" },
    },
    {
      path: "/screenshots/:tickId",
      name: "screenshot-detail",
      component: () => import("../pages/ScreenshotDetailPage.vue"),
      meta: { title: "截图详情", hidden: true, group: "cases", parent: "screenshots" },
    },
    {
      path: "/benchmarks",
      name: "benchmarks",
      component: () => import("../pages/BenchmarkPage.vue"),
      meta: { title: "Benchmark", icon: "TrendCharts", group: "cases" },
    },
    {
      path: "/experiments",
      name: "experiments",
      component: () => import("../pages/ExperimentListPage.vue"),
      meta: { title: "实验 A/B", icon: "Switch", group: "cases" },
    },
    {
      path: "/experiments/:id",
      name: "experiment-detail",
      component: () => import("../pages/ExperimentDetailPage.vue"),
      meta: { title: "实验详情", hidden: true, group: "cases", parent: "experiments" },
    },
    {
      path: "/code-audit",
      name: "code-audit",
      component: () => import("../pages/CodeAuditPage.vue"),
      meta: { title: "代码审计", icon: "DocumentChecked", group: "cases" },
    },
    {
      path: "/code-audit/:key",
      name: "code-audit-detail",
      component: () => import("../pages/CodeAuditDetailPage.vue"),
      meta: { title: "审计详情", hidden: true, group: "cases", parent: "code-audit" },
    },
    {
      path: "/wiki-review",
      name: "wiki-review",
      component: () => import("../pages/WikiReviewPage.vue"),
      meta: { title: "Wiki 审核", icon: "Notebook", group: "cases" },
    },
    {
      path: "/wiki-review/:id",
      name: "wiki-review-detail",
      component: () => import("../pages/WikiReviewDetailPage.vue"),
      meta: { title: "Wiki 条目", hidden: true, group: "cases", parent: "wiki-review" },
    },
  ],
});

router.afterEach((to) => {
  const title = (to.meta?.title as string | undefined) ?? "";
  document.title = title ? `${title} · WeChat RPA` : "WeChat RPA";
});

export default router;
