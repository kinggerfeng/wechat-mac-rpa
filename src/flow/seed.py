"""Seed flow: the existing bot main loop expressed as a graph.

This is the artefact that proves the engine can express what ``WeChatBot.tick()``
already does. The structure mirrors the tick decomposition of
``src/bot/wechat_bot.py`` step for step, including the branches that the
imperative code handles with early ``return``s — those become condition nodes and
named ports.

It is seeded into the store on first API boot. It is a *starting point*, not
read-only: edit it in the canvas, and the imperative bot is left untouched.
"""

from __future__ import annotations

from typing import Any

SEED_FLOW_ID = "flow_default_wechat_bot"
SEED_FLOW_NAME = "微信自动回复主循环"
DEMO_FLOW_ID = "flow_demo_dual_path"
DEMO_FLOW_NAME = "双路径定位演示"


def _node(node_id: str, node_type: str, name: str, x: float, y: float, **kw: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "name": name,
        "position": {"x": x, "y": y},
        "params": kw.pop("params", {}),
        "retry": kw.pop("retry", {"max": 0, "delay": 0}),
        "timeout": kw.pop("timeout", None),
        "on_error": kw.pop("on_error", "fail"),
        "outputs": kw.pop("outputs", []),
        "disabled": kw.pop("disabled", False),
        # 路径在配置流程时选定，写在节点上而不是 params 里
        "path": kw.pop("path", None),
        "target": kw.pop("target", None),
    }
    assert not kw, f"unexpected node keys: {sorted(kw)}"
    return node


def _edge(edge_id: str, source: str, target: str, port: str = "ok", condition: str | None = None, label: str = "") -> dict[str, Any]:
    return {
        "id": edge_id,
        "source": source,
        "source_port": port,
        "target": target,
        "condition": condition,
        "label": label,
    }


def build_default_graph() -> dict[str, Any]:
    """The main loop as a graph document."""
    nodes = [
        _node("n_start", "start", "开始", 40, 420),
        _node("n_tick", "set_var", "推进 Tick 计数", 200, 420,
              params={"name": "tick_id", "operation": "increment", "by": 1}),

        # ── perceive, with login recovery as the error branch ──
        _node("n_perceive", "perceive", "感知微信窗口", 380, 340,
              timeout=60, on_error="branch", retry={"max": 1, "delay": 2.0},
              outputs=["chat_name", "messages", "message_count", "screenshot_path"]),
        _node("n_login", "login_recovery", "处理登录界面", 380, 80,
              timeout=45, on_error="branch",
              outputs=["recovered", "status"]),
        _node("n_login_manual", "log", "等待人工登录", 620, 40,
              params={"message": "需要人工完成微信登录：{{status}}", "level": "error"}),
        _node("n_perceive_retry", "perceive", "重新感知", 620, 180,
              timeout=60, outputs=["chat_name", "messages", "message_count", "screenshot_path"]),

        # ── nothing perceived → log and idle ──
        _node("n_has_result", "condition", "有识别结果？", 620, 340,
              params={"expression": "perceive.ok == true"},
              outputs=["true", "false"]),
        _node("n_no_result", "log", "本轮无识别", 840, 440,
              params={"message": "感知返回空结果，跳过本轮", "level": "warn"}),

        # ── session merge ──
        _node("n_merge", "merge_session", "合并会话并去重", 840, 320,
              outputs=["unreplied", "unreplied_count"]),
        _node("n_has_unreplied", "condition", "有待回复消息？", 1060, 320,
              params={"expression": "merge_session.unreplied_count > 0"},
              outputs=["true", "false"]),

        # ── policy + silent gate ──
        _node("n_policy", "reply_policy", "回复策略过滤", 1280, 200,
              outputs=["allowed", "allowed_count", "filtered_count"]),
        _node("n_has_allowed", "condition", "策略通过？", 1500, 200,
              params={"expression": "reply_policy.allowed_count > 0"},
              outputs=["true", "false"]),
        _node("n_silent", "silent_gate", "静默白名单校验", 1720, 200,
              outputs=["whitelisted", "skip"]),

        # ── generate + record + send ──
        _node("n_generate", "generate_reply", "生成回复", 1720, 140,
              timeout=90, on_error="branch", retry={"max": 1, "delay": 3.0},
              outputs=["replies", "count"]),
        _node("n_no_reply", "log", "生成空回复", 1940, 40,
              params={"message": "生成器返回空回复", "level": "warn"}),
        _node("n_send", "send_message", "发送消息", 1940, 140,
              params={"text": "{{generate_reply.replies}}", "interval": 1.5},
              outputs=["sent_count", "results"]),
        _node("n_record", "record_tick", "记录 Tick", 2160, 140,
              outputs=["recorded"]),
        _node("n_send_result", "update_send_result", "回写发送结果", 2380, 140,
              outputs=["updated"]),
        _node("n_mark", "mark_replied", "标记已回复", 2600, 140,
              params={"mode": "all", "reply_text": "{{generate_reply.replies}}"},
              outputs=["marked"]),
        _node("n_memory", "memory_update", "更新记忆", 2820, 140,
              outputs=["queued"]),

        # ── skip recording path ──
        _node("n_record_skip", "record_tick", "记录跳过", 1500, 320,
              params={"skip_reason": "{{skip_reason}}"}, outputs=["recorded"]),

        # ── tail ──
        _node("n_switch", "switch_chat", "切到下一个未读", 3040, 260,
              path="element", target="wechat",
              params={"strategy": "unread"},
              outputs=["clicked", "nickname"]),
        _node("n_save", "save_session", "保存会话", 3260, 260, outputs=["saved"]),
        _node("n_wait", "wait", "等待下一轮", 3480, 260,
              params={"seconds": "{{interval}}"}, outputs=["waited"]),
        _node("n_end", "end", "结束", 3700, 260, params={"status": "ok"}),
    ]

    edges = [
        _edge("e01", "n_start", "n_tick"),
        _edge("e02", "n_tick", "n_perceive"),

        # perceive failed → try login recovery
        _edge("e03", "n_perceive", "n_login", port="error", label="微信未就绪"),
        _edge("e04", "n_login", "n_login_manual", port="manual", label="需人工"),
        _edge("e05", "n_login", "n_perceive_retry", port="ok", label="已恢复"),
        _edge("e06", "n_perceive_retry", "n_perceive", label="重新感知"),
        _edge("e07", "n_login_manual", "n_wait", label="跳过本轮"),
        _edge("e08", "n_perceive", "n_has_result", port="ok"),

        _edge("e09", "n_has_result", "n_merge", port="true"),
        _edge("e10", "n_has_result", "n_no_result", port="false"),
        _edge("e11", "n_no_result", "n_wait", label="空结果"),

        _edge("e12", "n_merge", "n_has_unreplied"),
        _edge("e13", "n_has_unreplied", "n_policy", port="true", label="有待回复"),
        _edge("e14", "n_has_unreplied", "n_record_skip", port="false", label="无需回复"),
        _edge("e15", "n_policy", "n_has_allowed"),
        _edge("e16", "n_has_allowed", "n_silent", port="true", label="策略通过"),
        _edge("e17", "n_has_allowed", "n_record_skip", port="false", label="被策略过滤"),
        _edge("e18", "n_silent", "n_record_skip", port="skip", label="非白名单"),
        _edge("e20", "n_silent", "n_generate", port="ok", label="白名单内"),

        _edge("e21", "n_generate", "n_no_reply", port="empty", label="空回复"),
        _edge("e22", "n_generate", "n_send", port="ok", label="有回复"),
        _edge("e23", "n_send", "n_record"),
        _edge("e24", "n_record", "n_send_result"),
        _edge("e25", "n_send_result", "n_mark"),
        _edge("e26", "n_mark", "n_memory"),
        _edge("e27", "n_memory", "n_switch"),
        _edge("e28", "n_no_reply", "n_record_skip", label="空回复记录"),
        _edge("e29", "n_record_skip", "n_switch", label="下一轮"),

        _edge("e30", "n_switch", "n_save"),
        _edge("e31", "n_save", "n_wait"),
        _edge("e32", "n_wait", "n_end"),
    ]

    return {
        "version": 1,
        "name": SEED_FLOW_NAME,
        "description": "由 WeChatBot.tick() 迁移而来的可视化主循环。感知 → 会话合并 → 策略 → 生成 → 发送 → 记录 → 记忆 → 下一轮。",
        "entry": "n_start",
        "default_path": "element",
        "variables": {"interval": 5.0, "tick_id": 0, "skip_reason": ""},
        "nodes": nodes,
        "edges": edges,
    }


def build_dual_path_demo() -> dict[str, Any]:
    """A side-by-side demo of both location paths.

    The main loop above is faithful to the existing bot. This flow exists to make
    the strategy choice visible in the canvas: run the same locate twice, once in
    ``element`` mode and once in ``auto``, and the trace shows which path actually
    produced each point and what it cost. This is the flow to open when someone
    asks "why did it click there?".

    WeChat is the honest example of why both paths are needed. Its message list
    has no inspectable structure at all — it is drawn by the app and read by
    multimodal perception. Its search box, input area and back button, by
    contrast, are perfectly nameable regions. A bot that only had one path would
    either pay a model call per click, or be unable to touch the message list.
    """
    # Layout note: the chain is serial, but it is laid out in three columns
    # rather than one 2500px row. At 4.3:1 a "fit" zoom lands near 0.25 and
    # every label is unreadable; folding the chain keeps the same topology at
    # a usable zoom. Coordinates are (x, y) pairs, still left-to-right per
    # column, with branch outcomes fanning ±100px off their parent.
    nodes = [
        _node("d_start", "start", "开始", 40, 380),
        _node("d_capture", "capture", "截图", 300, 380, outputs=["image_path", "scale_factor"]),

        # ── Path A：元素库 ──
        _node("d_locate_element", "locate", "路径A · 元素定位", 560, 380,
              on_error="branch", path="element_strict", target="wechat",
              params={"element": "搜索框"},
              outputs=["x", "y", "source", "confidence"]),
        _node("d_element_ok", "log", "元素命中", 820, 280,
              params={"message": "元素库命中：{{d_locate_element.source}} 置信度 {{d_locate_element.confidence}}"}),
        _node("d_element_fail", "log", "元素未命中", 820, 480,
              params={"message": "元素库未命中，且 element_strict 模式拒绝降级到模型", "level": "warn"}),

        # ── Path B：多模态 ──
        _node("d_locate_vision", "locate", "路径B · 视觉定位", 1080, 380,
              on_error="branch", path="vision", target="wechat",
              params={"description": "微信主界面左上角的搜索输入框", "confidence": 0.6},
              outputs=["x", "y", "source", "confidence"]),
        _node("d_vision_ok", "log", "视觉命中", 1340, 280,
              params={"message": "视觉模型命中：置信度 {{d_locate_vision.confidence}}"}),
        _node("d_vision_fail", "log", "视觉未命中", 1340, 480,
              params={"message": "视觉模型未找到该元素", "level": "error"}),

        # ── 合并：auto 模式自动二选一 ──
        _node("d_locate_auto", "locate", "自动 · 元素优先，失败降级", 560, 760,
              on_error="branch", path="auto", target="wechat",
              params={"element": "搜索框", "description": "微信主界面左上角的搜索输入框",
                      "confidence": 0.6},
              outputs=["x", "y", "source", "confidence"]),
        _node("d_auto_report", "log", "auto 走了哪条路", 880, 760,
              params={"message": "auto 模式实际来源：{{d_locate_auto.source}}"}),

        # ── 无结构界面只能多模态 ──
        _node("d_opaque", "vlm_act", "无结构界面 · 只能多模态", 1180, 760,
              on_error="branch", path="vision", target="any_window",
              params={"goal": "在当前界面找到并点击左上角的搜索框", "wait_seconds": 0.5},
              outputs=["decided", "performed"]),
        _node("d_opaque_done", "log", "多模态已完成", 1460, 700,
              params={"message": "模型判定：{{d_opaque.decided}}"}),
        _node("d_opaque_blocked", "log", "多模态受阻", 1460, 840,
              params={"message": "模型无法完成该目标", "level": "error"}),

        # ── 界面描述 → 引导沉淀为元素 ──
        _node("d_describe", "vlm_describe", "识别界面并沉淀元素", 880, 1120,
              on_error="branch", path="vision", target="wechat",
              params={"save_as_elements": True, "name_prefix": "wechat"},
              outputs=["summary", "regions", "saved"]),
        _node("d_verify", "vlm_verify", "前置断言", 1180, 1120,
              on_error="branch", path="vision", target="wechat",
              params={"claim": "当前界面是微信主窗口，聊天列表在左侧可见"}),
        _node("d_verify_ok", "log", "断言通过", 1460, 1060,
              params={"message": "可以继续执行后续步骤"}),
        _node("d_verify_fail", "log", "断言失败", 1460, 1180,
              params={"message": "界面状态与预期不符，停止", "level": "error"}),
        _node("d_end", "end", "结束", 1720, 1120, params={"status": "ok"}),
    ]

    edges = [
        _edge("de01", "d_start", "d_capture"),

        # 串行跑三遍同一个定位目标，把两条路径和 auto 的取舍都记录进轨迹。
        # 串行而不是并行分支，是因为一个节点同一出口只会走第一条边——串行
        # 才能在一条轨迹里同时看到"元素失败""视觉成功""auto 选了谁"。
        _edge("de02", "d_capture", "d_locate_element", label="① 路径A"),
        _edge("de03", "d_locate_element", "d_element_ok", port="ok", label="元素命中"),
        _edge("de04", "d_locate_element", "d_element_fail", port="error", label="元素未命中"),
        _edge("de05", "d_element_ok", "d_locate_vision", label="继续"),
        _edge("de06", "d_element_fail", "d_locate_vision", label="继续"),

        _edge("de07", "d_locate_vision", "d_vision_ok", port="ok", label="视觉命中"),
        _edge("de08", "d_locate_vision", "d_vision_fail", port="error", label="视觉未命中"),
        _edge("de09", "d_vision_ok", "d_locate_auto", label="继续"),
        _edge("de10", "d_vision_fail", "d_locate_auto", label="继续"),

        _edge("de11", "d_locate_auto", "d_auto_report", port="ok", label="auto 成功"),
        _edge("de12", "d_locate_auto", "d_auto_report", port="error", label="auto 也失败"),

        _edge("de13", "d_auto_report", "d_opaque", label="④ 无结构界面"),
        _edge("de14", "d_opaque", "d_opaque_done", port="ok", label="已执行"),
        _edge("de15", "d_opaque", "d_opaque_done", port="done", label="目标已达成"),
        _edge("de16", "d_opaque", "d_opaque_blocked", port="blocked", label="受阻"),
        _edge("de17", "d_opaque", "d_opaque_blocked", port="error", label="模型不可用"),

        _edge("de18", "d_opaque_done", "d_describe", label="⑤ 沉淀为元素"),
        _edge("de19", "d_opaque_blocked", "d_describe", label="继续"),
        _edge("de20", "d_describe", "d_verify", port="ok", label="描述完成"),
        _edge("de21", "d_describe", "d_verify", port="error", label="模型不可用"),

        _edge("de22", "d_verify", "d_verify_ok", port="ok", label="断言成立"),
        _edge("de23", "d_verify", "d_verify_fail", port="fail", label="断言不成立"),
        _edge("de24", "d_verify", "d_verify_fail", port="error", label="模型不可用"),
        _edge("de25", "d_verify_ok", "d_end"),
        _edge("de26", "d_verify_fail", "d_end"),
    ]

    return {
        "version": 1,
        "name": DEMO_FLOW_NAME,
        "description": "并排演示两条定位路径：路径A 元素库（本地、确定性、免费）与路径B 多模态（适配无结构界面）。路径在配置流程时于节点上选定；auto 表示元素优先、失败降级到模型。",
        "entry": "d_start",
        "default_path": "element",
        "variables": {},
        "nodes": nodes,
        "edges": edges,
    }


def seed(store: Any) -> int:
    """Write the seed flows if none exist yet. Returns how many were written."""
    if store.list_flows():
        return 0
    main = build_default_graph()
    store.save_flow(SEED_FLOW_ID, SEED_FLOW_NAME, main, description=main["description"], is_active=True)
    demo = build_dual_path_demo()
    store.save_flow(DEMO_FLOW_ID, DEMO_FLOW_NAME, demo, description=demo["description"])
    return 2
