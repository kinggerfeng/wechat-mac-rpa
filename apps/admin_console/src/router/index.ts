import { createRouter, createWebHashHistory } from "vue-router";

/**
 * The operations console. Every route here reads `data/cases.db` and is about
 * judging what the bot already did — which is why this is a separate app from
 * the desktop shell rather than a hidden group in it: an end user of the RPA
 * product has no such job, and shipping these pages to them only produces
 * links that 404.
 *
 * Hash history for the same reason the desktop shell uses it, plus one more:
 * this is served by a dev server or a static host, and a deep link must not
 * depend on the server rewriting paths.
 */
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/ticks" },
    {
      path: "/ticks",
      name: "ticks",
      component: () => import("../pages/TickListPage.vue"),
      meta: { title: "Tick 记录", icon: "List" },
    },
    {
      path: "/ticks/:id",
      name: "tick-detail",
      component: () => import("../pages/TickDetailPage.vue"),
      meta: { title: "Tick 详情", parent: "ticks" },
    },
    {
      path: "/ground-truth",
      name: "ground-truth",
      component: () => import("../pages/GroundTruthPage.vue"),
      meta: { title: "真值对比", icon: "ScaleToOriginal" },
    },
    {
      path: "/reviews",
      name: "reviews",
      component: () => import("../pages/ReviewListPage.vue"),
      meta: { title: "案例库", icon: "Collection" },
    },
    {
      path: "/screenshots",
      name: "screenshots",
      component: () => import("../pages/ScreenshotListPage.vue"),
      meta: { title: "截图", icon: "Picture" },
    },
    {
      path: "/screenshots/:tickId",
      name: "screenshot-detail",
      component: () => import("../pages/ScreenshotDetailPage.vue"),
      meta: { title: "截图详情", parent: "screenshots" },
    },
    {
      path: "/benchmarks",
      name: "benchmarks",
      component: () => import("../pages/BenchmarkPage.vue"),
      meta: { title: "Benchmark", icon: "TrendCharts" },
    },
    {
      path: "/experiments",
      name: "experiments",
      component: () => import("../pages/ExperimentListPage.vue"),
      meta: { title: "实验 A/B", icon: "Switch" },
    },
    {
      path: "/experiments/:id",
      name: "experiment-detail",
      component: () => import("../pages/ExperimentDetailPage.vue"),
      meta: { title: "实验详情", parent: "experiments" },
    },
    {
      path: "/code-audit",
      name: "code-audit",
      component: () => import("../pages/CodeAuditPage.vue"),
      meta: { title: "代码审计", icon: "DocumentChecked" },
    },
    {
      path: "/code-audit/:key",
      name: "code-audit-detail",
      component: () => import("../pages/CodeAuditDetailPage.vue"),
      meta: { title: "审计详情", parent: "code-audit" },
    },
    {
      path: "/wiki-review",
      name: "wiki-review",
      component: () => import("../pages/WikiReviewPage.vue"),
      meta: { title: "Wiki 审核", icon: "Notebook" },
    },
    {
      path: "/wiki-review/:id",
      name: "wiki-review-detail",
      component: () => import("../pages/WikiReviewDetailPage.vue"),
      meta: { title: "Wiki 条目", parent: "wiki-review" },
    },
  ],
});

router.afterEach((to) => {
  const title = (to.meta?.title as string | undefined) ?? "";
  document.title = title ? `${title} · 运营平台` : "运营平台";
});

export default router;
