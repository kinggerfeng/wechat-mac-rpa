# 前端改动流程与硬约束

> 改 `app/` 时加载。本文只记**会踩、且踩了会静默出错**的坑。
> 与 `debugging.md`（改 `rpa/` 时）并列，两者都服从 `CLAUDE.md` 的铁律。

## 技术栈与硬约束

| 项 | 值 |
|---|---|
| 框架 | Vue 3 `<script setup lang="ts">`，**不用 Options API** |
| 路由 | `createWebHashHistory()` —— Tauri 从文件系统加载，path history 刷新必 404 |
| 组件库 | Element Plus，**不新增依赖** |
| 图标 | `@element-plus/icons-vue` |
| 样式 | scoped `<style>`，颜色一律取 `rpa/styles/tokens.css` 的 CSS 变量 |
| 静态检查 | `npx vue-tsc --noEmit -p tsconfig.json` 必须退出 0 |

**不许自造配色**。tokens 里已有 `--green / --orange / --line / --paper / --canvas /
--path-element / --path-vision / --path-auto` 等。新页面硬写 `#357b63` 就是在制造
第二套主题。

**文案中文，注释和标识符英文。** 注释只写「为什么这么定」，不写「这行在干什么」。

## 改前端前必读

| 文件 | 抄它的什么 |
|---|---|
| `app/rpa/pages/RunHistoryPage.vue` | 列表页骨架：loading / loadError / empty 三态 |
| `app/rpa/pages/CanvasPage.vue` | 两栏布局、长耗时动作的进度反馈 |
| `app/rpa/api/client.ts` | 所有请求的唯一入口，**页面里不许裸 `fetch`** |
| `app/rpa/types/index.ts` | 手写契约，**不许 `any` 糊过去** |

---

## 坑 1：API 失败渲染成空列表（本项目头号 bug 的前端形态）

后端头号 bug 是「静默返回错误结果」。前端同一类 bug 是把**请求失败**画成**没有数据**。

两者在页面上长得几乎一样，运维会照着空列表做判断，然后去查一个根本不存在的问题。

```ts
// 错：失败和空无法区分
const rows = ref<T[]>([]);
try { rows.value = (await api.ticks()).rows } catch { /* 吞掉 */ }
```

**判据**：每个数据页必须能回答「我现在看到的是**真的没有**，还是**没拿到**？」

必须显式维护 `loading` / `loadError` / `rows` 三个状态，三者互斥渲染。
`loadError` 里的文案要说清是哪个操作失败，不能是「加载失败」四个字了事。

同一条适用于**写操作**：保存失败绝不能乐观地渲染成已保存。失败的 `el-alert` 不要自动消失。

---

## 坑 2：nullable 渲染成 0

后端大量字段是 `number | null`（`judge_score` / `confidence` / `c_avg`…）。

`{{ row.judge_score || 0 }}` 和 `{{ row.judge_score ?? "—" }}` 的区别就是
「这一条没评分」和「这一条评了 0 分」的区别 —— 后者会直接改变运维的判断。

`NaN` 同理：算 delta 时任一侧是 null，差值也必须是 dash，不能是 `NaN`。
JSON 里根本没有 `NaN`，出现它说明后端算错了或前端漏了守卫。

---

## 坑 3：路由参数全是 string

`useRoute().params.id` 的类型是 `string | string[]`，永远是字符串。

- 数字 id（`/ticks/:id`）必须先 `Number(...)` 并校验是正整数，非法就渲染「链接无效」
  并**不发请求**，而不是把 `NaN` 塞进 URL 让后端 404
- 字符串 key（`/code-audit/:key`）必须 `encodeURIComponent` 编码、
  `decodeURIComponent` 解码，且**不要**顺手做 `toLowerCase()` 之类的规范化 ——
  后端是精确匹配的，规范化会打到另一个 key 上

**404 和传输失败是两个状态**。后端返回 404 说明「这东西不存在」，
网络失败说明「我联系不上后端」，给用户的动作完全不同。

---

## 坑 4：`v-html` 与外部 HTML

只有一处允许 `v-html`：Benchmark 报告。但那份报告是**外部脚本生成的完整
HTML 文档**（自带 `<html>/<style>`），用裸 `v-html` 渲染会把它的 CSS 泄进整个控制台。

正解是 `<iframe :srcdoc>` 隔离，另给一个「源码」视图看原始 HTML。
它是本地可信产物，所以可以渲染；但「可信」不等于「该让它改我的样式」。

反过来，Wiki 审核的 `context` 是**后端已经转义好的纯文本**，
必须 `<pre>` + `white-space: pre`，**绝不能** `v-html` ——
再解释一次就把后端的转义作废了。

---

## 坑 5：画布 HTML5 drag 无法用合成事件驱动

节点卡片用的是原生 `draggable="true"`，`mousedown/mousemove/mouseup` 合成事件
**不会**触发 `dragstart` / `drop`。用 Playwright 或 Browser 工具
「拖一个节点进画布」是做不到的，会误以为功能坏了。

验证画布交互有两条路：

1. 直接调 API 建流程，再打开画布 —— 绕过拖拽
2. 真的用 `open -a` 起 RPAStudio.app，人工拖

**不要**因为拖不进去就去改 `draggable` 的实现，那是另一个人的文件在用。

---

## 坑 6：节点缺 `position` 会让整页白屏

graph 里的节点若缺 `position`，`NodeCard.vue` 读 `node.position.x` 直接抛
`TypeError: Cannot read properties of undefined (reading 'x')`，
**整个页面白屏**。

而 API 的 `validate` 和 `PUT /flows/{id}` 都**接受**这种 graph。
也就是说：能存进去，跑起来会炸，页面还打不开。

（新画布的保存路径已经会补 `position`；从旧数据或外部导入仍可能遇到。
真遇到先确认是不是这个，不要去改 `NodeCard.vue` 的判空。）

---

## 坑 7：`#/` 直接跳子路由时顶栏误报「离线」

`App.vue` 的「LOCAL API 已连接 / 离线」指示灯，在**首次进入非 overview 页面**时
可能显示离线，而 API 实际 200 —— 因为首帧的轮询还没回来。

**验证离线状态要看 network，不是看这个指示灯。** 页面能正常渲染就说明 API 通。

---

## 提交前自查

```bash
cd app
npx vue-tsc --noEmit -p tsconfig.json   # 必须退出 0
npm run build                            # 必须成功
```

两条都过 ≠ 功能对。改了交互就**真的点一遍**：
空态、错误态、长文本、null 字段、非法路由参数。

跑 build 失败时先确认是不是别人正在写的页面还没落地 ——
**不要**为了让 build 过而删掉自己以外的 import。
