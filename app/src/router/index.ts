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
  ],
});

router.afterEach((to) => {
  const title = (to.meta?.title as string | undefined) ?? "";
  document.title = title ? `${title} · WeChat RPA` : "WeChat RPA";
});

export default router;
