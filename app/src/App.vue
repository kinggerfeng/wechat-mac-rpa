<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { Bell, CircleCheck, Refresh, VideoPlay, VideoPause } from "@element-plus/icons-vue";

const apiBase = "http://127.0.0.1:8767";
const activeView = ref("overview");
const status = ref(null);
const logs = ref([]);
const apiOnline = ref(false);
const busy = ref(false);
const errorMessage = ref("");
const lastUpdated = ref("");
let refreshTimer;

const running = computed(() => status.value?.running === true);
const statusLabel = computed(() => {
  if (!apiOnline.value) return "后端未连接";
  return running.value ? "运行中" : "已停止";
});

async function apiRequest(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

async function refreshData() {
  try {
    const [nextStatus, nextLogs] = await Promise.all([
      apiRequest("/api/status"),
      apiRequest("/api/logs?lines=200"),
    ]);
    status.value = nextStatus;
    logs.value = nextLogs.logs;
    apiOnline.value = true;
    errorMessage.value = "";
    lastUpdated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (error) {
    apiOnline.value = false;
    errorMessage.value = error.message || "无法连接本地 Python 服务";
  }
}

async function startBot() {
  busy.value = true;
  errorMessage.value = "";
  try {
    await apiRequest("/api/bot/start", { method: "POST" });
    await refreshData();
  } catch (error) {
    errorMessage.value = error.message || "Bot 启动失败";
  } finally {
    busy.value = false;
  }
}

async function stopBot() {
  busy.value = true;
  errorMessage.value = "";
  try {
    await apiRequest("/api/bot/stop", { method: "POST" });
    await refreshData();
  } catch (error) {
    errorMessage.value = error.message || "Bot 停止失败";
  } finally {
    busy.value = false;
  }
}

async function reconnect() {
  try {
    await invoke("start_backend");
  } catch {
    // A backend already launched by Tauri setup is still reachable.
  }
  await refreshData();
}

onMounted(() => {
  reconnect();
  refreshTimer = window.setInterval(refreshData, 3000);
});

onUnmounted(() => window.clearInterval(refreshTimer));
</script>

<template>
  <main class="shell">
    <aside class="rail">
      <div class="brand-mark"><span>W</span></div>
      <div class="rail-rule"></div>
      <button class="rail-button selected" title="运行总览" @click="activeView = 'overview'">
        <el-icon><CircleCheck /></el-icon>
      </button>
      <button class="rail-button" title="运行日志" @click="activeView = 'logs'">
        <el-icon><Bell /></el-icon>
      </button>
      <div class="rail-bottom">RPA<br /><b>DESK</b></div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="breadcrumbs"><span>WECHAT MAC RPA</span><i>/</i><b>控制台</b></div>
        <div class="top-meta">
          <span class="api-state" :class="{ online: apiOnline }"><span></span>{{ apiOnline ? 'LOCAL API 已连接' : 'LOCAL API 离线' }}</span>
          <span class="version">桌面预览 · 0.1.0</span>
        </div>
      </header>

      <div class="content">
        <section class="page-heading">
          <div>
            <p class="eyebrow">BOT OPERATIONS / 01</p>
            <h1>运行控制</h1>
            <p class="subheading">Python 引擎状态与本机运行日志</p>
          </div>
          <el-button class="refresh-button" :icon="Refresh" circle aria-label="刷新状态" @click="refreshData" />
        </section>

        <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="notice" />

        <section class="status-band">
          <div class="status-main">
            <div class="status-orbit" :class="{ active: running, offline: !apiOnline }"><span></span></div>
            <div>
              <p class="eyebrow">ENGINE STATUS</p>
              <h2>{{ statusLabel }}</h2>
              <p class="status-caption">{{ running ? '机器人进程正在本机运行' : '机器人当前未执行自动化任务' }}</p>
            </div>
          </div>
          <div class="status-facts">
            <div><span>进程 PID</span><b>{{ status?.pid || '—' }}</b></div>
            <div><span>模型</span><b>{{ status?.model || '—' }}</b></div>
            <div><span>微信连接</span><b class="muted-value">{{ status?.wechat_status || '待检测' }}</b></div>
          </div>
          <div class="status-actions">
            <el-button v-if="!running" type="primary" :icon="VideoPlay" :loading="busy" :disabled="!apiOnline" @click="startBot">启动 Bot</el-button>
            <el-button v-else type="danger" plain :icon="VideoPause" :loading="busy" @click="stopBot">停止 Bot</el-button>
          </div>
        </section>

        <section class="section-heading">
          <div>
            <p class="eyebrow">ACTIVITY STREAM</p>
            <h2>{{ activeView === 'logs' ? '运行日志' : '最近活动' }}</h2>
          </div>
          <span class="updated">更新于 {{ lastUpdated || '等待连接' }}</span>
        </section>

        <section class="log-surface" aria-label="Bot 运行日志">
          <div class="log-toolbar"><span class="log-dot"></span><span>desktop-bot.log</span><span class="log-count">{{ logs.length }} 行</span></div>
          <div v-if="logs.length" class="log-lines">
            <div v-for="(line, index) in logs" :key="`${index}-${line}`" class="log-line">
              <span class="line-number">{{ String(index + 1).padStart(3, '0') }}</span>
              <code>{{ line }}</code>
            </div>
          </div>
          <div v-else class="empty-logs">
            <span class="empty-mark">—</span>
            <p>{{ apiOnline ? '暂无运行日志' : '等待 Python 后端连接' }}</p>
          </div>
        </section>

        <footer class="page-footer"><span>服务地址</span><code>127.0.0.1:8767</code><span class="footer-separator"></span><span>自动刷新 · 3 秒</span></footer>
      </div>
    </section>
  </main>
</template>

<style>
:root {
  font-family: "Avenir Next", "SF Pro Display", sans-serif;
  color: #182322;
  background: #edf1ed;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  --ink: #182322;
  --muted: #788581;
  --line: #dce3de;
  --green: #357b63;
  --green-dark: #1f5546;
  --orange: #cf714c;
  --paper: #f8faf7;
  letter-spacing: 0;
}

* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; min-height: 100vh; }
button { font: inherit; }
.shell { display: flex; min-height: 100vh; }
.rail { width: 76px; flex: 0 0 76px; min-height: 100vh; display: flex; flex-direction: column; align-items: center; background: #193b34; color: #e6eee8; padding: 22px 0 18px; }
.brand-mark { width: 34px; height: 34px; display: grid; place-items: center; border: 1px solid #729282; color: #fff; font-family: Georgia, serif; font-size: 20px; font-style: italic; }
.rail-rule { width: 28px; height: 1px; margin: 26px 0 15px; background: #46665a; }
.rail-button { width: 42px; height: 42px; margin: 5px 0; display: grid; place-items: center; border: 0; color: #99b0a3; background: transparent; cursor: pointer; }
.rail-button .el-icon { font-size: 19px; }
.rail-button:hover, .rail-button.selected { color: #fff; background: #2c5d4d; }
.rail-bottom { margin-top: auto; color: #87a194; font-size: 9px; line-height: 1.5; letter-spacing: 1px; text-align: center; }
.rail-bottom b { color: #e2eee6; font-weight: 600; }
.workspace { min-width: 0; flex: 1; }
.topbar { height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 36px; border-bottom: 1px solid var(--line); background: rgba(248,250,247,.86); }
.breadcrumbs, .top-meta { display: flex; align-items: center; gap: 13px; }
.breadcrumbs { color: #8a9691; font-size: 10px; letter-spacing: 1px; }
.breadcrumbs i { color: #bdc7c1; font-style: normal; }
.breadcrumbs b { color: #33443f; font-size: 12px; font-weight: 600; letter-spacing: 0; }
.api-state { display: flex; align-items: center; gap: 7px; color: #9a6551; font-size: 10px; letter-spacing: .3px; }
.api-state > span { width: 6px; height: 6px; border-radius: 50%; background: var(--orange); }
.api-state.online { color: var(--green); }
.api-state.online > span { background: #52a27c; }
.version { padding-left: 13px; border-left: 1px solid var(--line); color: #8c9893; font-size: 10px; }
.content { width: min(1120px, 100%); margin: 0 auto; padding: 42px 42px 24px; }
.page-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 27px; }
.eyebrow { margin: 0 0 9px; color: #809087; font-size: 9px; font-weight: 700; letter-spacing: 1.45px; }
h1, h2, p { margin-top: 0; }
h1 { margin-bottom: 5px; font-family: Georgia, "Times New Roman", serif; font-size: 32px; font-weight: 500; line-height: 1.15; }
.subheading { margin-bottom: 0; color: #84908b; font-size: 12px; }
.refresh-button { border-color: #dce3de; color: #66766f; background: #f8faf7; }
.notice { margin: -8px 0 18px; }
.status-band { display: grid; grid-template-columns: minmax(210px, 1.1fr) minmax(290px, 1fr) auto; align-items: center; gap: 22px; min-height: 168px; padding: 25px 28px; border: 1px solid #d9e2dc; border-left: 3px solid var(--green); background: var(--paper); box-shadow: 0 8px 24px rgba(29,62,49,.035); }
.status-main { display: flex; align-items: center; gap: 17px; }
.status-orbit { width: 52px; height: 52px; flex: 0 0 52px; display: grid; place-items: center; border: 1px solid #d4ddd7; border-radius: 50%; }
.status-orbit span { width: 13px; height: 13px; border-radius: 50%; background: #a5b1aa; }
.status-orbit.active { border-color: #a5d0b9; box-shadow: 0 0 0 5px rgba(82,162,124,.08); }
.status-orbit.active span { background: #52a27c; box-shadow: 0 0 12px rgba(82,162,124,.55); }
.status-orbit.offline span { background: #cf714c; }
.status-main .eyebrow { margin-bottom: 6px; }
.status-main h2 { margin: 0 0 4px; font-family: Georgia, "Times New Roman", serif; font-size: 22px; font-weight: 500; }
.status-caption { margin: 0; color: #87938d; font-size: 11px; }
.status-facts { display: grid; grid-template-columns: repeat(3, minmax(65px, 1fr)); gap: 14px; padding: 4px 0 4px 22px; border-left: 1px solid var(--line); }
.status-facts div { min-width: 0; }
.status-facts span, .status-facts b { display: block; }
.status-facts span { margin-bottom: 9px; color: #8a9790; font-size: 10px; }
.status-facts b { overflow: hidden; color: #35443e; font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; font-weight: 500; text-overflow: ellipsis; }
.status-facts .muted-value { color: #9a7667; font-family: inherit; font-size: 11px; }
.status-actions { justify-self: end; }
.status-actions .el-button { min-width: 112px; height: 38px; border-radius: 3px; font-size: 12px; }
.status-actions .el-button--primary { --el-color-primary: #357b63; --el-color-primary-light-3: #65a18b; --el-color-primary-dark-2: #1f5546; }
.section-heading { display: flex; align-items: end; justify-content: space-between; margin: 36px 0 13px; }
.section-heading .eyebrow { margin-bottom: 6px; }
.section-heading h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: 20px; font-weight: 500; }
.updated { padding-bottom: 2px; color: #8b9691; font-size: 10px; }
.log-surface { min-height: 290px; overflow: hidden; border: 1px solid #dce3de; background: #fbfcfa; }
.log-toolbar { height: 39px; display: flex; align-items: center; gap: 9px; padding: 0 14px; border-bottom: 1px solid #e4e9e5; color: #718079; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; }
.log-dot { width: 7px; height: 7px; border-radius: 50%; background: #cf714c; }
.log-count { margin-left: auto; color: #a0aaa4; }
.log-lines { max-height: 340px; overflow: auto; padding: 10px 0; }
.log-line { min-height: 25px; display: grid; grid-template-columns: 48px minmax(0, 1fr); align-items: start; padding: 4px 14px 4px 0; }
.log-line:hover { background: #f1f5f1; }
.line-number { padding: 1px 12px 0 0; color: #b0b9b3; font-family: "SFMono-Regular", Consolas, monospace; font-size: 9px; text-align: right; user-select: none; }
.log-line code { overflow-wrap: anywhere; color: #4e5d56; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; line-height: 1.55; white-space: pre-wrap; }
.empty-logs { min-height: 248px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #9aa59e; }
.empty-mark { display: grid; width: 28px; height: 28px; margin-bottom: 9px; place-items: center; border: 1px solid #dce3de; border-radius: 50%; color: #809087; }
.empty-logs p { margin: 0; font-size: 11px; }
.page-footer { display: flex; align-items: center; gap: 9px; margin-top: 17px; color: #8b9691; font-size: 10px; }
.page-footer code { color: #52635b; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; }
.footer-separator { height: 11px; margin: 0 3px; border-left: 1px solid #d0d9d2; }
.el-alert { border-radius: 3px; }

@media (max-width: 760px) {
  .rail { width: 56px; flex-basis: 56px; }
  .topbar { height: 54px; padding: 0 17px; }
  .version { display: none; }
  .content { padding: 30px 18px 22px; }
  .status-band { grid-template-columns: 1fr auto; gap: 21px 12px; padding: 20px; }
  .status-main { grid-column: 1 / -1; }
  .status-facts { grid-column: 1 / 2; padding-left: 0; border-left: 0; }
  .status-actions { grid-column: 2 / 3; grid-row: 2; }
  .status-actions .el-button { min-width: 98px; padding: 0 10px; }
  .section-heading { margin-top: 28px; }
  h1 { font-size: 29px; }
}

@media (max-width: 430px) {
  .breadcrumbs { gap: 7px; font-size: 8px; }
  .api-state { font-size: 0; }
  .api-state > span { width: 8px; height: 8px; }
  .status-band { grid-template-columns: minmax(0, 1fr); }
  .status-main, .status-facts, .status-actions { grid-column: 1; grid-row: auto; }
  .status-actions { justify-self: start; }
  .status-band { padding: 17px 14px; }
  .status-facts { gap: 8px; }
  .status-facts span { font-size: 9px; }
  .status-facts b { font-size: 10px; }
  .updated { font-size: 9px; }
}
</style>
