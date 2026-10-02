# CLAUDE.md

> wechat-mac-rpa · 每次会话全量加载 · < 150 行

## Karpathy 铁律

1. **先澄清再实现**：不确定需求先问。禁止脑补用户意图。
2. **越简单越好**：3 行能解决不写 30 行。不改无关代码。
3. **手术式修改**：只动目标代码。禁止顺手重构、格式化、加注释。
4. **验证再报告**：改完必须跑测试/截图确认。禁止"应该没问题"。

## 修改流程

1. Read 目标文件，确认当前状态
2. 口头说明改什么、为什么、预期效果
3. 等用户明确同意（"好""改""执行"）
4. 最小化修改，执行
5. 验证 → 报告结果（失败就说失败）

## 架构不变量

违反这些不是「能跑就行」，是设计错误。改动前先确认没有违反：

- **调度器单实例**：多进程共享 `data/rpa.db` 时，只有抢到文件锁的进程启动调度线程。
  否则同一条 cron 触发 N 次，重复发消息、重复截图
- **节点零业务逻辑**：只包装既有子系统并整形返回值；判断逻辑留在原模块（保留其测试）
- **双路径是设计期选择**：`path` 是 `Node` 一等字段，不是运行时兜底旋钮。
  `element_strict` 元素失败即致命，绝不调模型
- **RPA 域用 `data/rpa.db`**，不复用 `cases.db`（后者有 3 处已确认缺陷）
- **代码执行只有一个入口**：`run_shell` 默认 `shlex.split` 免 shell。
  不引入容器沙箱，本机 RPA Studio 定位不同于多人 SaaS
- **权限授予的是 bundle**：`/Applications/RPAStudio.app`，不是 python 解释器。
  从 bash 起的进程 TCC 归属是 MiniMax Code，测权限必须用 `open` 从 bundle 启动

## 禁止事项

- 未经同意执行修改、脚本、重启服务
- "顺便"改无关代码、删除现有功能
- 编造不确定的信息（说"不确定"）
- 用猜代替验证（先复现最小 case 对比）
- **把"没报错"当"对了"**——静默返回错误结果是本项目头号 bug 来源

## 项目速查

```
src/bot/wechat_bot.py          # L5 主循环
src/flow/                      # RPA 编排引擎（新增，与 wechat_bot.py 并存不替换）
  registry.py                  # 节点注册表，41 个节点
  expr.py                      # ast 白名单表达式求值器（禁用 eval）
  executor.py                  # 执行器
  strategy.py                  # 双路径定位唯一入口 resolve()
src/reply/generator.py         # L4 回复生成
src/badcase/judge_worker.py    # Judge 评分
src/perception/smart_pipeline.py # L3.5 感知
src/memory/engine.py           # L4 记忆
src/action/system_automation.py # RPA 原语 ABC（macOS 实现 + NoOp）
src/capture/window_capture.py  # 窗口截图
scripts/admin.py               # 管理后台 :8766
data/persona.md                # Bot 私人人设（Git 忽略）
data/rpa.db                    # RPA 域数据（不与 cases.db 混用）
```

## 规则分层

| 文件 | 加载时机 | 内容 |
|------|---------|------|
| `CLAUDE.md` | 每次会话 | 核心铁律 + 速查 |
| `.claude/rules/frontend.md` | 改 admin.py 时 | Playwright 验证 |
| `.claude/rules/debugging.md` | 改 src/ 时 | 调试流程 + 常见坑 |
