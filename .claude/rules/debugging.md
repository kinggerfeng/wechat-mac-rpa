# 调试流程与常见坑

> 改 `rpa/` 时加载。本文只记**实测踩过、且一定会再踩**的坑。
> 通用推测不写进来。

## 调试流程

1. 先复现最小 case，再动代码
2. 改完必须有测试锁死行为，测试要断言**新语义**而不是旧实现的副作用
3. 真机验证前先问用户——微信在本机运行，真实执行会发真实消息
4. 报告分三档：完全实现 / 占位 / 没做。「已修 / 缺漏 / 设计偏差」分开列

---

## 坑 1：静默返回错误结果（本项目头号 bug 来源）

已修的四个真实案例，同一个模式——**不报错，但结果是错的**：

| 位置 | 表现 |
|---|---|
| `schema.CONDITION_RE` 的 `(.+?)` 懒惰匹配 | `count > 0 and x == 1` 后半截被吞成字面量 → `3 > nan` = False，**守卫静默失效** |
| `eval({"__builtins__": {}})` | 实测 `().__class__.__bases__[0].__subclasses__()` 可达 **225 个已加载类** |
| `_validate_wechat_screenshot` 的 `bool(left_text)` | 「非空即通过」，任何有文字的应用都算微信 |
| `_do_capture` 的静默降级 | `-l` 失败降级 `-R`，微信被遮挡仍返回 ok |

**判据**：任何「拿不到就退一步」的分支，都要问一句——**退这一步之后，调用方能分辨吗？**
不能分辨就不是降级，是 bug。

这条同时解释了为什么表达式求值器改成 `ast` 白名单而不是继续修正则：
正则结构上无法表达 `and` 和括号，前缀匹配必然在某处静默返回错误结果。

---

## 坑 2：macOS 权限只在特定进程树成立

实测（2026-10-02）：

```
bash 直启 uvicorn         → screen_recording: denied → screencapture 三次全失败
RPAStudio.app → 8768      → granted → 能出图
```

- 权限授予的是 **bundle**（`/Applications/RPAStudio.app`），不是 python 解释器
- **从 bash 起的进程，TCC 归属是 MiniMax Code**——会弹「MiniMax Code 想要录制此电脑的屏幕和音频」，
  并且**污染测量结果**。早期 5 次权限测试全从 bash 启动，测出的 denied 是假的，
  当时据此得出「必须买 Developer ID 证书」的错误结论
- **系统设置列表里有该应用 ≠ 判定通过**，两者对不上是常态（`ax_trusted` 可见翻转）
- 测权限必须用 `open` 从目标 bundle 启动，不能 `cd && python`
- 弹框频繁出现 = 有进程在反复调 `screencapture`，先查调度器再查代码

---

## 坑 3：screencapture -l 对微信必然失败

四次实测（激活前 / 0.5s / 1.5s / 3.0s）全部 `could not create image from window`。
透明合成层导致，**区域截图 `-R` 是本机唯一能出图的路径**，不是「降级选项」。

`-R` 读的是**屏幕**不是窗口。所以微信被遮挡时会截到遮挡物，且旧代码返回 ok。
`_do_capture` 因此必须先用 `get_frontmost_app()` 校验前台，不对就抛错——
区域截图只能在确认微信确实在前台时才可信。

---

## 坑 4：多实例调度器会重复执行

`data/rpa.db` 被多个 uvicorn 进程共享时，每个进程都加载同一张 `schedules` 表，
同一条 cron 触发 N 次。实测三个进程（8767 / 8768 / 8769）各跑一次
`flow_demo_dual_path`，该流程第二个节点就是 `capture` → 每 15 分钟弹 3 次权限框。

**调度器必须单实例**：启动时抢文件锁，抢不到就不启动调度线程。
没有这个约束，多实例部署下必然重复发消息、重复截图。

---

## 坑 5：tesseract 没有 chi_sim

本机 tesseract 只有 `eng/osd/snum`。代码里写 `lang='chi_sim+eng'` 会直接抛异常，
落进 `except Exception` 后行为取决于异常处理——旧代码在这里返回 `False`，
和「真的不是微信」无法区分。

中文 OCR 用项目自带 `VisionOCREngine`（macOS Vision 框架，零新增依赖，实测带 bbox）。

---

## 坑 6：2x Retina 下硬编码像素会切空

截图物理像素是逻辑尺寸的 2 倍。写 `crop((0, 0, 400, 80))` 实际只覆盖逻辑 200×40，
而搜索框在物理像素 x≈166 起——刚好切不到。
裁剪区域必须按 `scale_factor` 换算，或干脆不裁剪。

---

## 坑 7：临时目录不是 /tmp

macOS 的 `tempfile.gettempdir()` 是 `/var/folders/.../T/`。
排查产物时去 `/tmp` 找会扑空。
