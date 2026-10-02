"""Built-in node types.

Each node is a thin adapter over an existing subsystem. The rule for every node
here: **no business logic**. If a node needs a decision, that decision belongs in
an existing module and the node just calls it and shapes the return value. A flow
graph that re-implements reply policy in YAML has lost the tests that module has.

Nodes raise :class:`~src.flow.schema.NodeError` for expected runtime conditions
(WeChat not open, capture rejected, LLM refused) so ``on_error: branch`` can route
them, and let genuinely unexpected exceptions propagate.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .executor import branch
from .registry import BaseNode, NodeRegistry, ParamSpec
from .schema import NodeAborted, NodeError

#: Vision scroll directions → wheel clicks. Only vertical is expressible as a
#: wheel gesture: macOS trackpads have no horizontal wheel, and synthesising
#: one would mean a horizontal drag, which is a different gesture with
#: different side effects on a list. Left/right is refused rather than faked.
SCROLL_CLICKS = {"up": 3, "down": -3}

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────────────────────────────────────────── control ──

class StartNode(BaseNode):
    """Flow entry point. Reports what triggered the run."""

    def execute(self) -> dict[str, Any]:
        trigger = self.ctx.scope.get("__trigger__")
        return {"started_at": time.time(), "trigger": trigger or "manual"}


class EndNode(BaseNode):
    """Terminal node. Optionally fails the run with ``message``."""

    def execute(self) -> dict[str, Any]:
        message = self.param("message", "")
        status = str(self.param("status", "ok"))
        if status == "error":
            raise NodeError(message or "流程以失败状态结束")
        return {"ended_at": time.time(), "message": message}


class WaitNode(BaseNode):
    """Pause the run. The main loop uses this instead of a hard-coded sleep.

    Sleeps in short slices so a stop request is honoured within a second instead
    of after the full wait — the main loop waits 5s per tick and a desktop user
    should not have to wait out a long interval to stop the bot.
    """

    _SLICE = 0.25

    def execute(self) -> dict[str, Any]:
        # {{interval}} resolves in param(); interpolating again here would
        # expand a value that legitimately contains braces a second time.
        seconds = float(self.param("seconds", 5.0) or 0.0)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ctx.abort_requested:
                raise NodeAborted("等待期间收到停止请求")
            time.sleep(min(self._SLICE, max(0.0, deadline - time.monotonic())))
        return {"waited": seconds}


class SetVarNode(BaseNode):
    """Bind a computed value into the run scope.

    ``operation=increment`` bumps a numeric variable, which is how a flow keeps a
    tick counter without writing ``tick_id + 1`` in an expression and hoping the
    variable exists.

    ``expression`` is evaluated by :mod:`src.flow.expr`, which walks a parsed
    ``ast`` through an allow-list. It used to be handed to :func:`eval` with a
    reduced ``__builtins__``, on the belief that a closed namespace is a
    sandbox. It is not: ``().__class__.__bases__[0].__subclasses__()`` still
    reaches every loaded class under that namespace, and a flow graph is
    editable through the API, so the expression was an arbitrary-execution
    primitive rather than a convenience.
    """

    def execute(self) -> dict[str, Any]:
        name = str(self.param("name", "")).strip()
        if not name:
            raise NodeError("set_var 需要 name 参数")
        operation = str(self.param("operation", "set"))
        if operation == "increment":
            by = float(self.param("by", 1) or 1)
            current = self.ctx.scope.get(name)
            value = (float(current) if isinstance(current, (int, float)) else 0.0) + by
        else:
            expression = self.param("expression", "")
            if not expression:
                value = self.param("value")
            else:
                value = self._eval(expression)
        self.ctx.scope.bind(name, value)
        return {name: value}

    def _eval(self, expression: str) -> Any:
        from .expr import evaluate
        from .schema import FlowError

        try:
            return evaluate(str(expression), self.ctx.scope)
        except FlowError as exc:
            raise NodeError(f"表达式求值失败: {exc}") from exc


class LogNode(BaseNode):
    """Write a line to the run trace. ``level`` is informational only."""

    def execute(self) -> dict[str, Any]:
        text = str(self.param("message", ""))
        level = str(self.param("level", "info"))
        try:
            self.ctx.service("logger").log_decision(
                text, skip_reason=text if level != "info" else None
            )
        except Exception:  # noqa: BLE001 - logging must never fail a run
            pass
        return {"message": text, "level": level}


class ConditionNode(BaseNode):
    """Branch on a boolean expression.

    ``expression`` is a full expression evaluated by :mod:`src.flow.expr` against
    the run scope — comparisons, ``and``/``or``/``not``, parentheses, literals and
    a whitelisted function set. Ports: ``true`` / ``false``.
    """

    def execute(self) -> dict[str, Any]:
        from .context import evaluate_condition
        from .schema import FlowError

        expression = str(self.param("expression", "")).strip()
        if not expression:
            raise NodeError("condition 需要 expression 参数")
        try:
            value = evaluate_condition(expression, self.ctx.scope)
        except FlowError as exc:
            raise NodeError(f"条件表达式求值失败: {exc}") from exc
        # The executor routes on this key: it is how a node chooses a port without
        # raising, so the span is recorded as a normal success.
        return {"__branch__": "true" if value else "false", "value": value, "expression": expression}


# ────────────────────────────────────────────────────────── perception ──

class CaptureNode(BaseNode):
    """Screenshot the WeChat window without interpreting it."""

    def execute(self) -> dict[str, Any]:
        from src.capture.window_capture import WeChatNotReadyError

        try:
            result = self.ctx.service("capture").capture()
        except WeChatNotReadyError as exc:
            raise NodeError(f"微信未就绪: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise NodeError(f"截图失败: {exc}") from exc
        rect = result.window_rect
        self.ctx.scope.bind("window_rect", (rect.x, rect.y, rect.width, rect.height))
        self.ctx.scope.bind("scale_factor", result.scale_factor)
        return {
            "image_path": result.image_path,
            "screenshot_path": result.image_path,
            "window_rect": (rect.x, rect.y, rect.width, rect.height),
            "scale_factor": result.scale_factor,
        }


class PerceiveNode(BaseNode):
    """Run the full perception pipeline: capture → OCR/VLM → layout → extraction.

    Ports: ``ok`` (a result came back), ``empty`` (nothing perceived this tick),
    ``error`` (WeChat not ready / not open).
    """

    def execute(self) -> dict[str, Any]:
        from src.capture.window_capture import WeChatNotReadyError

        try:
            result = self.ctx.service("perception").perceive()
        except WeChatNotReadyError as exc:
            raise NodeError(f"微信未就绪: {exc}") from exc
        if result is None:
            return {"__branch__": "empty", "ok": False, "message_count": 0, "reason": "perception returned None"}
        if result.window_rect is not None:
            rect = result.window_rect
            self.ctx.scope.bind("window_rect", (rect.x, rect.y, rect.width, rect.height))
            self.ctx.scope.bind("scale_factor", result.scale_factor)
        payload = {
            "__branch__": "ok",
            "ok": True,
            "chat_name": result.chat_name,
            "is_group": result.is_group,
            "message_count": len(result.messages),
            "messages": [_message_to_dict(m) for m in result.messages],
            "chat_list_count": len(result.chat_list_items),
            "chat_list": [_chat_item_to_dict(i) for i in result.chat_list_items],
            "screenshot_path": result.screenshot_path,
            "is_service_account_list": result.is_service_account_list,
        }
        return payload


class LoginRecoveryNode(BaseNode):
    """Detect the WeChat login screen and click through it.

    Ports: ``ok`` (logged in), ``manual`` (QR or phone confirmation — a human has
    to act), ``none`` (no login screen and no login button found).
    """

    def execute(self) -> dict[str, Any]:
        from src.action.login_recovery import LoginRecoveryStatus, WeChatLoginHandler

        handler = WeChatLoginHandler(automation=self.ctx.service("automation"))
        result = handler.handle()
        status = result.status
        value = status.value if isinstance(status, LoginRecoveryStatus) else str(status)
        needs_human = value in (
            LoginRecoveryStatus.NEEDS_QRCODE.value,
            LoginRecoveryStatus.NEEDS_PHONE_CONFIRM.value,
        )
        port = (
            "ok" if value == LoginRecoveryStatus.SUCCESS.value
            else "manual" if needs_human
            else "none"
        )
        return {
            "__branch__": port,
            "recovered": value == LoginRecoveryStatus.SUCCESS.value,
            "status": value,
            "needs_human": needs_human,
            "message": result.message,
        }


class SwitchChatNode(BaseNode):
    """Click a chat in the left list, or return to the list view.

    ``strategy`` is one of ``unread`` (first unread item), ``nickname`` (match
    ``target_nickname``), ``back`` (close the current chat).
    """

    def execute(self) -> dict[str, Any]:
        from src.action.chat_list_clicker import ChatListClicker
        from src.models.base import ChatListItem, Rect

        strategy = str(self.param("strategy", "unread"))
        clicker = self._clicker()

        if strategy == "back":
            ok = bool(clicker.click_back_button())
            return {"__branch__": "ok" if ok else "none", "clicked": ok, "strategy": strategy}

        raw_items = self.ctx.scope.get("chat_list") or []
        items = [
            ChatListItem(
                nickname=str(i.get("nickname", "")),
                last_message_preview=str(i.get("last_message_preview", "")),
                unread_count=str(i.get("unread_count", "")),
                timestamp=str(i.get("timestamp", "")),
                rect=Rect(*[int(v) for v in i.get("rect", (0, 0, 0, 0))]),
            )
            for i in raw_items
            if isinstance(i, dict)
        ]
        if not items:
            return {"__branch__": "none", "clicked": False, "strategy": strategy, "reason": "no chat list items"}

        if strategy == "nickname":
            target = str(self.param("target_nickname", "")).strip()
            if not target:
                raise NodeError("switch_chat 需要 target_nickname 参数")
            index = next((i for i, item in enumerate(items) if target in item.nickname), -1)
            if index < 0:
                return {"__branch__": "none", "clicked": False, "strategy": strategy, "reason": f"未找到会话 {target!r}"}
            ok = bool(clicker.click_by_index(items, index))
            return {"__branch__": "ok" if ok else "none", "clicked": ok, "strategy": strategy, "nickname": target}

        clicked = clicker.click_first_unread(items)
        if clicked is not None:
            return {"__branch__": "ok", "clicked": True, "strategy": strategy, "nickname": clicked.nickname}
        return {"__branch__": "none", "clicked": False, "strategy": strategy, "reason": "no unread items"}

    def _clicker(self) -> Any:
        from src.action.chat_list_clicker import ChatListClicker

        profile = self.ctx.service("profile")
        rect = self.ctx.scope.get("window_rect") or (0, 0, profile.window_width, profile.window_height)
        return ChatListClicker(
            window_rect=rect,
            scale_factor=float(self.ctx.scope.get("scale_factor") or 2.0),
            automation=self.ctx.service("automation"),
        )


# ───────────────────────────────────────────────────────────── session ──

class MergeSessionNode(BaseNode):
    """Merge this tick's messages into the persistent session, deduping repeats."""

    def execute(self) -> dict[str, Any]:
        from src.utils.chat_utils import _normalize_chat_name

        perception_chat = str(self.ctx.scope.get("chat_name") or "")
        chat_name = str(self.param("chat_name", "") or perception_chat)
        if not chat_name:
            return {"merged": False, "reason": "no chat name in scope"}
        messages = self.ctx.scope.get("messages") or []
        store = self.ctx.service("session")
        chat_state, unreplied = store.merge_tick(
            _normalize_chat_name(chat_name),
            self._as_chat_messages(messages),
            mode=str(self.param("mode", "ocr")),
            is_group=bool(self.ctx.scope.get("is_group", False)),
        )
        return {
            "merged": True,
            "chat_name": _normalize_chat_name(chat_name),
            "unreplied_count": len(unreplied),
            "unreplied": [_message_to_dict(m) for m in unreplied],
        }

    @staticmethod
    def _as_chat_messages(raw: list[Any]) -> list[Any]:
        from src.models.base import ChatMessage, SenderType

        out: list[ChatMessage] = []
        for item in raw:
            if isinstance(item, ChatMessage):
                out.append(item)
            elif isinstance(item, dict):
                out.append(
                    ChatMessage(
                        text=item.get("text", ""),
                        sender=item.get("sender", ""),
                        sender_type=SenderType(item.get("sender_type", "other")),
                        chat_name=item.get("chat_name", ""),
                        is_at_me=bool(item.get("is_at_me", False)),
                        timestamp=item.get("timestamp"),
                        message_type=item.get("message_type", "text"),
                        image_description=item.get("image_description", ""),
                        quoted_text=item.get("quoted_text", ""),
                    )
                )
        return out


class GetUnrepliedNode(BaseNode):
    """Load the pending messages for a chat from the session store."""

    def execute(self) -> dict[str, Any]:
        chat_name = str(self.param("chat_name", "") or self.ctx.scope.get("chat_name") or "")
        if not chat_name:
            return {"messages": [], "count": 0, "reason": "no chat name"}
        pending = self.ctx.service("session").get_unreplied(chat_name)
        return {"messages": [_message_to_dict(m) for m in pending], "count": len(pending), "chat_name": chat_name}


class MarkRepliedNode(BaseNode):
    """Mark pending messages as answered so the next tick does not repeat them.

    ``mode=all`` marks every pending message; ``mode=text`` marks the single
    message whose text matches ``target_text``.
    """

    def execute(self) -> dict[str, Any]:
        chat_name = str(self.param("chat_name", "") or self.ctx.scope.get("chat_name") or "")
        if not chat_name:
            return {"marked": 0, "reason": "no chat name"}
        store = self.ctx.service("session")
        mode = str(self.param("mode", "all"))
        reply_text = str(self.param("reply_text", "") or self.ctx.scope.get("reply_text") or "")
        pending = store.get_unreplied(chat_name)
        if mode == "text":
            target = str(self.param("target_text", ""))
            match = next((m for m in pending if m.text == target), None)
            if match is None:
                return {"marked": 0, "reason": "target_text not found"}
            store.mark_replied(chat_name, match, reply_text)
            return {"marked": 1, "mode": mode}
        store.mark_replied(chat_name, pending[0] if pending else None, reply_text)
        return {"marked": len(pending), "mode": "all"}


class SaveSessionNode(BaseNode):
    """Flush session state and screenshots to disk. Call at the end of a tick."""

    def execute(self) -> dict[str, Any]:
        self.ctx.service("session").save()
        return {"saved": True, "at": time.time()}


# ────────────────────────────────────────────────────────────── reply ──

class ReplyPolicyNode(BaseNode):
    """Apply :class:`ReplyPolicy` to each pending message.

    Ports: ``ok`` (at least one message may be answered), ``filtered`` (none).
    """

    def execute(self) -> dict[str, Any]:
        from src.models.base import SenderType

        policy = self.ctx.service("policy")
        pending = self.ctx.scope.get("unreplied") or []
        allowed = [m for m in pending if _should_reply(policy, m)]
        return {
            "__branch__": "ok" if allowed else "filtered",
            "allowed": [_message_to_dict(m) for m in allowed],
            "allowed_count": len(allowed),
            "filtered_count": len(pending) - len(allowed),
            "senders": sorted({str(m.get("sender", "")) for m in pending if isinstance(m, dict)}),
        }


class SilentGateNode(BaseNode):
    """Port ``skip`` when the chat is outside the silent-mode whitelist.

    Silent mode means the bot does not speak first; the whitelist is the set of
    chats it is allowed to speak in unprompted.
    """

    def execute(self) -> dict[str, Any]:
        chat_name = str(self.param("chat_name", "") or self.ctx.scope.get("chat_name") or "")
        whitelisted = bool(self.ctx.service("sender").in_silent_whitelist(chat_name))
        return {
            "__branch__": "ok" if whitelisted else "skip",
            "chat_name": chat_name,
            "whitelisted": whitelisted,
            "skip": not whitelisted,
        }


class GenerateReplyNode(BaseNode):
    """Produce reply text with :class:`ReplyGenerator`.

    Ports: ``ok`` (non-empty replies), ``empty`` (generator returned nothing).
    """

    def execute(self) -> dict[str, Any]:
        from src.models.base import ChatMessage, SenderType

        generator = self.ctx.service("generator")
        pending_raw = self.ctx.scope.get("unreplied") or []
        all_raw = self.ctx.scope.get("messages") or []
        unreplied = [_dict_to_message(m, pending_raw) for m in pending_raw if isinstance(m, dict)]
        all_messages = [_dict_to_message(m, all_raw) for m in all_raw if isinstance(m, dict)]
        if not unreplied:
            return {"__branch__": "empty", "replies": [], "count": 0, "reason": "nothing unreplied"}
        replies = generator.generate(
            unreplied,
            all_messages,
            is_group=bool(self.ctx.scope.get("is_group", False)),
            tick_id=int(self.ctx.scope.get("tick_id", 0) or 0),
        )
        replies = list(replies or [])
        return {"__branch__": "ok" if replies else "empty", "replies": replies, "count": len(replies)}


# ───────────────────────────────────────────────────────────── action ──

class SendMessageNode(BaseNode):
    """Type and send text into the currently open WeChat chat.

    ``text`` accepts ``{{variable}}`` interpolation against the run scope.
    Ports: ``ok``, ``skipped`` (silent mode / not whitelisted), ``error``.
    """

    def execute(self) -> dict[str, Any]:
        text = str(self.param("text", ""))
        if not text.strip():
            raise NodeError("send_message 的 text 为空")
        chat_name = str(self.param("chat_name", "") or self.ctx.scope.get("chat_name") or "")
        gap = float(self.param("interval", 1.5) or 0.0)
        results: list[dict[str, Any]] = []
        for index, line in enumerate(text.split("\n\n")):
            if index and gap:
                time.sleep(gap)
            result = self.ctx.service("sender").send(line, chat_name)
            results.append(
                {"sent": line[:80], "success": result.success, "skipped": result.skipped, "error": result.error}
            )
        failures = [r for r in results if not r["success"] and not r["skipped"]]
        if failures and len(failures) == len(results):
            raise NodeError(f"全部发送失败: {failures[0].get('error')}")
        skipped = any(r["skipped"] for r in results)
        return {
            "__branch__": "skipped" if skipped and not any(r["success"] for r in results) else "ok",
            "sent_count": len([r for r in results if r["success"]]),
            "skipped": skipped,
            "results": results,
        }


class ClickNode(BaseNode):
    """Click a screen point.

    Three ways to say where, in precedence order:

    1. ``x``/``y`` — an absolute point, or one bound by a preceding ``locate``.
    2. ``element`` — a name in the element library (Path A).
    3. ``description`` — free text, resolved by the vision model (Path B).

    When both ``element`` and ``description`` are set, ``mode`` decides; ``auto``
    prefers the element library and only calls a model if the element is missing.
    """

    def execute(self) -> dict[str, Any]:
        automation = self.ctx.service("automation")
        x, y = self.param("x"), self.param("y")
        if x is None or y is None:
            bound_x, bound_y = self.ctx.scope.get("located_x"), self.ctx.scope.get("located_y")
            if bound_x is not None and bound_y is not None:
                x, y = bound_x, bound_y

        if x is not None and y is not None:
            return {
                "clicked": bool(automation.click_at(int(x), int(y))),
                "x": int(x), "y": int(y), "source": "fixed",
            }

        from .strategy import resolve

        element = str(self.param("element", "")).strip()
        description = str(self.param("description", "")).strip()
        if not element and not description:
            raise NodeError("click 需要 x/y、element 或 description")
        located = resolve(
            self.ctx,
            description=description or element,
            target=self.target_name(),
            mode=self.locate_mode(),
            element=element or None,
            confidence=float(self.param("confidence", 0.6) or 0.6),
        )
        return {
            "clicked": bool(automation.click_at(located.x, located.y)),
            **{k: v for k, v in located.to_dict().items() if k in ("x", "y", "source", "confidence", "element_name", "label")},
        }


class PointerNode(BaseNode):
    """Pointer gesture at a resolved point: hover, right-click, double-click,
    drag, scroll.

    The point is resolved exactly the way :class:`ClickNode` resolves it — an
    explicit ``x``/``y``, a name in the element library, or free text handed to
    the vision model. Duplicating that here instead of extracting a helper was
    tempting, but :class:`ClickNode` predates the three-way scheme and shares
    its precedence rules with the recorder; keeping the resolution in one
    module means a change to element or vision resolution cannot land on one
    gesture and miss the other. The gesture itself is the only new part.

    ``drag`` and ``scroll`` need two points and one point respectively, so
    they take ``x2``/``y2`` and ``clicks`` respectively and ignore the rest.
    """

    #: Set by the registration below, one concrete gesture per node type. A
    #: ``gesture`` param would let a saved flow change meaning by editing a
    #: dropdown, and the canvas has no way to show which arms are live.
    gesture = "hover"

    def _resolve_point(self, automation: Any) -> tuple[int, int, dict[str, Any]]:
        x, y = self.param("x"), self.param("y")
        if x is None or y is None:
            bound_x, bound_y = self.ctx.scope.get("located_x"), self.ctx.scope.get("located_y")
            if bound_x is not None and bound_y is not None:
                x, y = bound_x, bound_y
        if x is not None and y is not None:
            return int(x), int(y), {"source": "fixed"}

        from .strategy import resolve

        element = str(self.param("element", "") or "").strip()
        description = str(self.param("description", "") or "").strip()
        if not element and not description:
            raise NodeError(f"{type(self).__name__} 需要 x/y、element 或 description")
        located = resolve(
            self.ctx,
            description=description or element,
            target=self.target_name(),
            mode=self.locate_mode(),
            element=element or None,
            confidence=float(self.param("confidence", 0.6) or 0.6),
        )
        return located.x, located.y, located.to_dict()

    def execute(self) -> dict[str, Any]:
        automation = self.ctx.service("automation")
        kind = self.gesture
        x, y, located = self._resolve_point(automation)
        base: dict[str, Any] = {"x": x, "y": y, "gesture": kind}

        if kind == "hover":
            base["moved"] = bool(automation.move_to(x, y))
        elif kind == "right_click":
            base["clicked"] = bool(automation.click_at(x, y, button="right"))
        elif kind == "double_click":
            base["clicked"] = bool(automation.click_at(x, y, count=2))
        elif kind == "drag":
            x2, y2 = self.param("x2"), self.param("y2")
            if x2 is None or y2 is None:
                raise NodeError("drag 需要 x2/y2")
            base["dropped"] = bool(
                automation.drag_to(x, y, int(x2), int(y2),
                                   duration_ms=int(self.param("duration_ms", 500) or 500))
            )
            base["x2"], base["y2"] = int(x2), int(y2)
        elif kind == "scroll":
            clicks = int(self.param("clicks", 0) or 0)
            if clicks == 0:
                raise NodeError("scroll 需要非零 clicks（正数向上，负数向下）")
            base["scrolled"] = bool(automation.scroll_at(x, y, clicks))
            base["clicks"] = clicks
        else:
            raise NodeError(f"未知手势 {kind!r}")

        for key in ("source", "confidence", "element_name", "label"):
            if key in located:
                base[key] = located[key]
        return base


class TypeKeysNode(BaseNode):
    """Send keystrokes through cliclick, or paste ``text`` via the clipboard."""

    def execute(self) -> dict[str, Any]:
        automation = self.ctx.service("automation")
        text = str(self.param("text", ""))
        if text:
            automation.set_clipboard_text(text)
            ok = bool(automation.send_keys("cmd+v"))
            return {"typed": text[:80], "success": ok, "mode": "clipboard"}
        keys = str(self.param("keys", ""))
        if not keys:
            raise NodeError("type_keys 需要 text 或 keys")
        return {"success": bool(automation.send_keys(keys)), "keys": keys, "mode": "keys"}


class ActivateAppNode(BaseNode):
    """Bring an application to the front. Default ``WeChat``."""

    def execute(self) -> dict[str, Any]:
        app = str(self.param("app", "WeChat"))
        return {"activated": bool(self.ctx.service("automation").activate_app(app)), "app": app}


class WindowRectNode(BaseNode):
    """Read the main window's position and size."""

    def execute(self) -> dict[str, Any]:
        from src.models.base import Rect

        app = str(self.param("app", "WeChat"))
        ok, rect, err = self.ctx.service("automation").get_window_rect(app)
        if not ok or rect is None:
            return {"found": False, "app": app, "reason": err or "未找到窗口",
                    "x": None, "y": None, "width": None, "height": None}
        return {"found": True, "app": app, "x": rect.x, "y": rect.y,
                "width": rect.width, "height": rect.height}


class SetWindowRectNode(BaseNode):
    """Move and resize the main window."""

    def execute(self) -> dict[str, Any]:
        from src.models.base import Rect

        app = str(self.param("app", "WeChat"))
        rect = Rect(
            x=int(self.param("x", 0) or 0),
            y=int(self.param("y", 0) or 0),
            width=int(self.param("width", 0) or 0),
            height=int(self.param("height", 0) or 0),
        )
        if rect.width <= 0 or rect.height <= 0:
            # A zero-sized window is unrecoverable through the UI, and macOS
            # silently accepts it — better to refuse than to hand the operator
            # an invisible application.
            raise NodeError(f"窗口宽高必须为正: {rect.width}x{rect.height}")
        ok = bool(self.ctx.service("automation").set_window_rect(app, rect))
        return {"moved": ok, "app": app, "x": rect.x, "y": rect.y,
                "width": rect.width, "height": rect.height}


class _WindowActionNode(BaseNode):
    """Shared plumbing for the single-call window actions."""

    #: ABC method name, and the output key that reports it. Declared as a pair
    #: rather than derived from the method name: ``minimize_window`` stripped
    #: of its suffix gives ``minimize``, not the ``minimized`` the canvas shows.
    action = ""
    result_key = ""

    def execute(self) -> dict[str, Any]:
        app = str(self.param("app", "WeChat"))
        method = getattr(self.ctx.service("automation"), self.action)
        return {self.result_key: bool(method(app)), "app": app}


class MinimizeWindowNode(_WindowActionNode):
    action = "minimize_window"
    result_key = "minimized"


class MaximizeWindowNode(_WindowActionNode):
    action = "maximize_window"
    result_key = "maximized"


class CloseWindowNode(_WindowActionNode):
    action = "close_window"
    result_key = "closed"


class SetClipboardNode(BaseNode):
    """Put text on the system clipboard."""

    def execute(self) -> dict[str, Any]:
        text = str(self.param("text", ""))
        return {"success": bool(self.ctx.service("automation").set_clipboard_text(text)), "length": len(text)}


# ───────────────────────────────────────────────────────────── memory ──

class MemorySearchNode(BaseNode):
    """Search the local memory wiki."""

    def execute(self) -> dict[str, Any]:
        query = str(self.param("query", ""))
        if not query:
            raise NodeError("memory_search 需要 query")
        top_k = int(self.param("top_k", 5) or 5)
        max_chars = int(self.param("max_chars", 4000) or 4000)
        text = self.ctx.service("memory").search_keyword(query, max_chars=max_chars, return_scored=False)
        return {"query": query, "result": text, "found": bool(text) and "未在本地记忆" not in text, "top_k": top_k}


class MemoryUpdateNode(BaseNode):
    """Queue a wiki update for a user or group. Async; returns immediately."""

    def execute(self) -> dict[str, Any]:
        engine = self.ctx.service("memory")
        chat_name = str(self.param("chat_name", "") or self.ctx.scope.get("chat_name") or "")
        is_group = bool(self.param("is_group", self.ctx.scope.get("is_group", False)))
        messages = self.ctx.scope.get("messages") or []
        replies = self.ctx.scope.get("replies") or []
        texts = [m.get("text", "") for m in messages if isinstance(m, dict)]
        if is_group:
            engine.update_group_wiki(chat_name, chat_name, texts, replies)
        else:
            engine.update_user_wiki(chat_name, chat_name, texts, replies)
        return {"queued": True, "chat_name": chat_name, "is_group": is_group}


# ───────────────────────────────────────────────────────────── record ──

class RecordTickNode(BaseNode):
    """Write one row to ``tick_log``.

    This is the table the existing dashboard, review and benchmark pages all read,
    so a flow that does not call it produces a bot that is invisible to every
    existing tool.
    """

    _COLUMNS = {
        "session_id": str, "tick_id": int, "skip_reason": str, "chat_name": str, "is_group": int,
        "screenshot_path": str, "messages_count": int, "new_messages_count": int, "system_prompt": str,
        "user_prompt": str, "raw_response": str, "tool_calls_json": str, "tool_results_json": str,
        "session_input_messages_json": str, "session_output_unreplied_json": str, "should_reply": int,
        "replies_sent_json": str, "send_success": int, "send_duration_ms": int, "judge_score": float,
        "judge_is_badcase": int, "tokens_estimated": int, "duration_ms": int, "self_refine_applied": int,
        "iterate_count": int, "react_round_count": int,
    }

    def execute(self) -> dict[str, Any]:
        scope = self.ctx.scope.snapshot()
        values: dict[str, Any] = {}
        for column, kind in self._COLUMNS.items():
            override = self.param(column)
            raw = override if override not in (None, "") else scope.get(column)
            if raw in (None, ""):
                continue
            try:
                values[column] = kind(raw) if kind is not int or not isinstance(raw, bool) else int(raw)
            except (TypeError, ValueError):
                continue
        if "chat_name" not in values:
            values["chat_name"] = str(scope.get("chat_name") or "")
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn = self.ctx.service("case_db")._get_conn()
        try:
            conn.execute(f"INSERT INTO tick_log ({columns}) VALUES ({placeholders})", list(values.values()))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - a metrics write must not kill a tick
            return {"recorded": False, "error": str(exc)}
        finally:
            conn.close()
        return {"recorded": True, "columns": len(values)}


class UpdateSendResultNode(BaseNode):
    """Stamp the send outcome back onto the tick row just written."""

    def execute(self) -> dict[str, Any]:
        tick_id = int(self.param("tick_id", 0) or self.ctx.scope.get("tick_id", 0) or 0)
        if not tick_id:
            return {"updated": False, "reason": "no tick_id"}
        replies = self.ctx.scope.get("replies") or []
        success = bool(self.ctx.scope.get("send_success", False))
        duration = int(self.ctx.scope.get("send_duration_ms", 0) or 0)
        conn = self.ctx.service("case_db")._get_conn()
        try:
            conn.execute(
                "UPDATE tick_log SET replies_sent_json=?, send_success=?, send_duration_ms=? WHERE tick_id=?",
                (json.dumps(replies, ensure_ascii=False), int(success), duration, tick_id),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            return {"updated": False, "error": str(exc)}
        finally:
            conn.close()
        return {"updated": True, "tick_id": tick_id}


class _ImageBackedNode(BaseNode):
    """Shared resolution of the image a pixel operation should read.

    An explicit ``haystack`` wins; otherwise the most recent ``capture`` /
    ``perceive`` output in this run is used. A per-node variable would have
    to be repeated on every node in the chain, and the overwhelmingly common
    case is "match against the screenshot I just took".
    """

    def _haystack(self) -> str:
        explicit = str(self.param("haystack", "") or "").strip()
        if explicit:
            return explicit
        for name in ("screenshot_path", "image_path"):
            value = self.ctx.scope.get(name)
            if value and str(value).endswith((".png", ".jpg", ".jpeg")):
                return str(value)
        raise NodeError(
            f"{type(self).__name__} 需要 haystack 参数，"
            "或在本流程前面加一个 capture/perceive 节点提供截图"
        )


class FindImageNode(_ImageBackedNode):
    """Locate a template image inside a screenshot."""

    def execute(self) -> dict[str, Any]:
        from .vision_match import find_template

        needle = str(self.param("image", "")).strip()
        if not needle:
            raise NodeError("find_image 需要 image 参数")
        result = find_template(
            self._haystack(), needle,
            threshold=float(self.param("threshold", 0.85) or 0.85),
        )
        return result.to_dict()


class FindColorNode(_ImageBackedNode):
    """Locate the centre of a colour region."""

    def execute(self) -> dict[str, Any]:
        from .vision_match import find_color

        raw = str(self.param("rgb", "")).strip()
        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
        if len(parts) != 3:
            raise NodeError(f"rgb 需要三个分量（例：20,120,220），收到 {raw!r}")
        try:
            rgb = tuple(int(p) for p in parts)
        except ValueError as e:
            raise NodeError(f"rgb 必须是整数: {raw!r}") from e
        if not all(0 <= c <= 255 for c in rgb):
            raise NodeError(f"rgb 分量需在 0-255 之间: {raw!r}")
        return find_color(
            self._haystack(), rgb,  # type: ignore[arg-type]
            threshold=int(self.param("threshold", 30) or 30),
        ).to_dict()


class AssertPixelNode(_ImageBackedNode):
    """Assert a pixel's colour, or just read it."""

    def execute(self) -> dict[str, Any]:
        from .vision_match import pixel_at

        x = int(self.param("x", 0) or 0)
        y = int(self.param("y", 0) or 0)
        try:
            actual = pixel_at(self._haystack(), x, y)
        except (FileNotFoundError, IndexError, ValueError) as e:
            return {"matched": False, "r": None, "g": None, "b": None,
                    "actual": None, "reason": str(e)}

        base: dict[str, Any] = {"r": actual[0], "g": actual[1], "b": actual[2],
                                "actual": list(actual)}
        raw = str(self.param("rgb", "") or "").strip()
        if not raw:
            # No expectation given: this is a read, not an assertion.
            return {**base, "matched": True, "reason": "仅读取，未设置期望值"}

        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
        if len(parts) != 3:
            raise NodeError(f"rgb 需要三个分量（例：20,120,220），收到 {raw!r}")
        try:
            expected = tuple(int(p) for p in parts)
        except ValueError as e:
            raise NodeError(f"rgb 必须是整数: {raw!r}") from e

        tolerance = int(self.param("tolerance", 12) or 12)
        distance = sum((a - b) ** 2 for a, b in zip(actual, expected)) ** 0.5
        matched = distance <= tolerance
        return {
            **base,
            "matched": matched,
            "reason": (
                f"色差 {distance:.1f} ≤ 容忍 {tolerance}" if matched
                else f"色差 {distance:.1f} 超过容忍 {tolerance}（实际 {actual}，期望 {expected}）"
            ),
        }


class LocateNode(BaseNode):
    """Resolve a screen point through the configured path.

    This is the node that makes the dual-path strategy visible and editable: its
    ``mode`` parameter is exactly the decision, and the trace records which path
    won. Bind its outputs into a ``click`` node and the action is path-agnostic.

    ``element`` names a library element (Path A). ``description`` is free text
    the vision model resolves (Path B). With ``mode=auto`` and both supplied,
    the element wins and the model is never called.
    """

    def execute(self) -> dict[str, Any]:
        from .strategy import resolve

        description = str(self.param("description", ""))
        element = str(self.param("element", "")) or None
        if not description and not element:
            raise NodeError("locate 需要 description 或 element")
        located = resolve(
            self.ctx,
            description=description or element or "",
            target=self.target_name(),
            mode=self.locate_mode(),
            element=element,
            confidence=float(self.param("confidence", 0.6) or 0.6),
        )
        # Bind as absolute screen points so a downstream click can use them
        # directly without re-resolving through the target's window origin.
        self.ctx.scope.bind("located_x", located.x)
        self.ctx.scope.bind("located_y", located.y)
        return located.to_dict()


class VisionActNode(BaseNode):
    """Path B — let a vision model decide and perform the next action.

    Use this for screens with no capturable structure. It is a single step by
    design: one model call, one action, then the flow re-observes. Letting the
    model plan several steps in one shot is how a bot ends up clicking through a
    confirmation dialog it never read.
    """

    #: Action -> automation method on SystemAutomation.
    _DISPATCH = {
        "click": "click_at",
        "double_click": "click_at",
        "right_click": "click_at",
    }

    def execute(self) -> dict[str, Any]:
        from .vision import plan_action

        goal = str(self.param("goal", ""))
        if not goal:
            raise NodeError("vlm_act 需要 goal")
        automation = self.ctx.service("automation")
        decision = plan_action(
            self.ctx,
            goal,
            target=self.target_name(),
            extra_context=str(self.param("context", "")),
        )
        outcome: dict[str, Any] = {"decided": decision.to_dict(), "performed": False}

        if decision.action in ("done", "blocked"):
            return {**outcome, "__branch__": decision.action}

        if decision.action in self._DISPATCH:
            if decision.x is None or decision.y is None:
                raise NodeError(f"模型选择 {decision.action} 但未给出坐标")
            automation.click_at(decision.x, decision.y)
            if decision.action == "double_click":
                time.sleep(0.08)
                automation.click_at(decision.x, decision.y)
            outcome.update({"performed": True, "x": decision.x, "y": decision.y})
        elif decision.action == "type":
            if decision.points:
                first = decision.points[0]
                automation.click_at(first["x"], first["y"])
                time.sleep(0.2)
            if decision.text:
                automation.set_clipboard_text(decision.text)
                # Previously the return value was dropped and `performed` was
                # reported True regardless, so a paste that AppleScript
                # rejected still looked like a success. Typing into the wrong
                # field is worse than an error the operator can see.
                pasted = bool(automation.send_keys("cmd+v"))
                if not pasted:
                    raise NodeError("粘贴失败：cmd+v 未生效，请检查辅助功能权限")
            outcome.update({"performed": True, "typed": decision.text[:120]})
        elif decision.action == "hotkey":
            ok = bool(automation.send_keys(decision.keys))
            outcome.update({"performed": ok, "keys": decision.keys})
        elif decision.action == "scroll":
            # The pointer layer has scroll now, so this is no longer something
            # to skip quietly: doing it and reporting failure is strictly
            # better than reporting success for a gesture that never happened.
            point = decision.points[0] if decision.points else None
            if point is None:
                raise NodeError(f"模型要求滚动 {decision.direction} 但未给出坐标")
            clicks = SCROLL_CLICKS.get(decision.direction)
            if clicks is None:
                raise NodeError(f"未知的滚动方向 {decision.direction!r}")
            scrolled = bool(automation.scroll_at(point["x"], point["y"], clicks))
            outcome.update({"performed": scrolled, "scroll": decision.direction, "clicks": clicks})
        elif decision.action == "wait":
            seconds = float(self.param("wait_seconds", 1.0) or 1.0)
            time.sleep(seconds)
            outcome.update({"performed": True, "waited": seconds})
        else:
            raise NodeError(f"模型返回了未知动作 {decision.action!r}")

        return {**outcome, "__branch__": "ok"}


class VisionDescribeNode(BaseNode):
    """Path B — name the regions on screen.

    Useful for bootstrapping the element library: run this on a screen once, and
    the returned regions can be saved as named elements, so subsequent runs use
    the free Path A instead of paying for a model call.
    """

    def execute(self) -> dict[str, Any]:
        from .vision import describe_screen

        result = describe_screen(self.ctx)
        as_elements = bool(self.param("save_as_elements", False))
        saved: list[str] = []
        if as_elements:
            from .store import get_store

            store = get_store()
            prefix = str(self.param("name_prefix", "region"))
            for index, region in enumerate(result["regions"], start=1):
                if not region.get("label"):
                    continue
                store.save_element({
                    "name": f"{prefix}_{index}_{_slug(region['label'])}",
                    "kind": "rect",
                    "rect": {
                        "x": region["x"], "y": region["y"],
                        "width": region["width"], "height": region["height"],
                        "relative_to": "screen",
                    },
                    "meta": {"source": "vlm_describe", "type": region.get("type", "")},
                })
                saved.append(f"{prefix}_{index}_{_slug(region['label'])}")
        return {"summary": result["summary"], "regions": result["regions"], "saved": saved}


class VisionVerifyNode(BaseNode):
    """Path B — check a claim about the screen.

    The cheap alternative to branching on a model-decided action: assert a
    precondition before the expensive step, instead of discovering the failure
    three nodes later.
    """

    def execute(self) -> dict[str, Any]:
        from .vision import verify

        claim = str(self.param("claim", ""))
        if not claim:
            raise NodeError("vlm_verify 需要 claim")
        result = verify(self.ctx, claim)
        return {"__branch__": "ok" if result["holds"] else "fail", **result}


# ──────────────────────────────────────────────────────────── utility ──

class HttpRequestNode(BaseNode):
    """HTTP request. Params: url, method, headers (JSON), body, timeout."""

    def execute(self) -> dict[str, Any]:
        url = str(self.param("url", ""))
        if not url:
            raise NodeError("http_request 需要 url")
        method = str(self.param("method", "GET")).upper()
        timeout = float(self.param("timeout", 20) or 20)
        headers = json.loads(str(self.param("headers", "{}") or "{}"))
        body = self.param("body", "")
        data = body.encode("utf-8") if isinstance(body, str) and body else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "ok": False, "body": exc.read().decode("utf-8", errors="replace")[:2000]}
        except Exception as exc:  # noqa: BLE001
            raise NodeError(f"HTTP 请求失败: {exc}") from exc
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            parsed = raw[:4000]
        return {"status": status, "ok": 200 <= status < 300, "body": parsed}


class FileNode(BaseNode):
    """Read or write a text file under the project root.

    Paths are resolved relative to the project root and cannot escape it — a flow
    can be triggered by a webhook, so ``body`` is operator input.
    """

    def execute(self) -> dict[str, Any]:
        mode = str(self.param("mode", "read"))
        raw_path = str(self.param("path", ""))
        if not raw_path:
            raise NodeError("file 需要 path")
        target = (PROJECT_ROOT / raw_path).resolve()
        if not str(target).startswith(str(PROJECT_ROOT.resolve())):
            raise NodeError("路径越出项目根目录，已拒绝")
        if mode == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = str(self.param("content", ""))
            target.write_text(content, encoding="utf-8")
            return {"written": True, "path": str(target.relative_to(PROJECT_ROOT)), "bytes": len(content.encode("utf-8"))}
        if not target.is_file():
            return {"read": False, "path": str(raw_path), "reason": "file not found", "content": ""}
        return {"read": True, "path": str(target.relative_to(PROJECT_ROOT)), "content": target.read_text(encoding="utf-8", errors="replace")[:20000]}


class LLMNode(BaseNode):
    """Call a chat model with a freely configured prompt.

    The existing :class:`GenerateReplyNode` is the only other way to reach a
    model, and it hard-wires the WeChat persona, the pending-message scope and
    the ``empty`` branch. That is the right shape for a reply bot and the wrong
    shape for "summarise this OCR text", so the general case gets its own node
    rather than growing parameters onto a domain node.

    Wraps :class:`src.llm.openclaw_client.OpenClawClient` instead of building a
    second transport: that client is already OpenAI-compatible, already handles
    the ``/v1`` suffix and the empty-content case, and is what the rest of the
    project talks to. No new dependency, and no model logic here — the node only
    shapes parameters and returns the result.
    """

    def execute(self) -> dict[str, Any]:
        prompt = str(self.param("prompt", "")).strip()
        if not prompt:
            raise NodeError("llm 需要 prompt")

        system = str(self.param("system", "") or "").strip()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Resolve the provider before the sampling params, so a provider's
        # defaults apply to anything the node left blank.
        provider_id = str(self.param("provider", "") or "").strip()
        provider = self._provider(provider_id) if provider_id else self._provider("")

        # Only send what was configured: passing temperature=None makes some
        # providers reject the request, and a 0 default would silently pin
        # every flow to greedy decoding.
        temperature = self.param("temperature", None)
        if temperature is None or temperature == "":
            temperature = (provider or {}).get("temperature")
        if temperature is None or temperature == "":
            temperature = None
        else:
            temperature = float(temperature)

        # ``max_tokens`` has a spec default, so a blank parameter comes back as
        # 1024 rather than empty; treat the spec default as "not set" so the
        # provider can supply it.
        max_tokens = int(self.param("max_tokens", 0) or 0)
        if max_tokens in (0, 1024):
            max_tokens = int((provider or {}).get("max_tokens") or 0) or max_tokens or 1024
        model = str(self.param("model", "") or "").strip()
        base_url = str(self.param("base_url", "") or "").strip()
        timeout = float(self.param("timeout", 0) or 0) or float((provider or {}).get("timeout") or 0) or 0

        client = self._client(model, base_url, max_tokens, system, provider)
        text = client.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout or None,
        )
        # chat() returns a message object when the model emitted tool calls.
        # No tools are offered here, so anything non-text is a provider
        # behaving unexpectedly and must not be reported as a reply.
        text = text if isinstance(text, str) else ""
        return {
            "text": text,
            "empty": not text.strip(),
            "model": getattr(client, "model", model),
        }

    def _client(
        self,
        model: str,
        base_url: str,
        max_tokens: int,
        system: str,
        provider: dict[str, Any] | None = None,
    ) -> Any:
        """Build the client from node parameters and provider config.

        Precedence: an explicit node parameter, then the provider row, then the
        shared ``llm`` service, then the client's own defaults. A provider
        carries the operator's gateway settings so a flow never hard-codes a
        key; a node parameter still wins so one flow can pin a different model
        without editing the provider everyone else shares.
        """
        from src.llm.openclaw_client import OpenClawClient

        kwargs: dict[str, Any] = {"max_tokens": max_tokens}
        if model:
            kwargs["model"] = model
        elif provider and provider.get("model"):
            kwargs["model"] = provider["model"]
        if base_url:
            kwargs["base_url"] = base_url
        elif provider and provider.get("base_url"):
            kwargs["base_url"] = provider["base_url"]
        if provider and provider.get("api_key"):
            kwargs["api_key"] = provider["api_key"]
        if system:
            kwargs["system_prompt"] = system

        # The shared client is an optimisation, not a requirement: a bare
        # FlowExecutor has no services registered, and a node that demands one
        # would fail there while working in the real runner. Absent service is
        # not an error.
        try:
            override = self.ctx.service("llm")
        except Exception:  # noqa: BLE001
            override = None
        if override is not None and not any(k in kwargs for k in ("model", "base_url")):
            return override
        return OpenClawClient(**kwargs)

    def _provider(self, provider_id: str) -> dict[str, Any] | None:
        """Look up a provider row, tolerating a store-less context.

        ``provider_id`` empty means "the default row". A named provider that
        does not exist is an error rather than a silent fall back to the
        default: the author selected a gateway, and quietly calling a
        different one is the exact class of bug this whole provider mechanism
        exists to remove.
        """
        try:
            from src.flow.store import get_store

            store = get_store()
        except Exception:  # noqa: BLE001
            return None
        try:
            if provider_id:
                row = store.get_provider(provider_id)
                if row is None:
                    raise NodeError(f"llm provider {provider_id!r} 不存在")
                return row
            return store.default_provider()
        except NodeError:
            raise
        except Exception:  # noqa: BLE001
            return None


class TemplateNode(BaseNode):
    """Render a Jinja2 template against the run scope.

    Jinja2's own ``{{name}}`` is the fast path's syntax, so the two compose:
    a template that only interpolates behaves identically either way, and
    ``{% for %}`` / ``{% if %}`` / filters are available when the text needs
    structure the interpolator deliberately does not have.

    Autoescape stays off. Output is assembled text for a chat window or a
    prompt, not HTML, and escaping would corrupt every ``&`` in a message
    being forwarded. The caller renders for a human, not for a browser.
    """

    #: ``StrictUndefined`` because a template that silently renders an empty
    #: string for a missing variable produces a subtly wrong message rather
    #: than an obvious failure — the same reasoning as the expression evaluator.
    def execute(self) -> dict[str, Any]:
        source = str(self.param("template", ""))
        if not source.strip():
            raise NodeError("template 需要 template 参数")

        from jinja2 import StrictUndefined
        from jinja2.sandbox import SandboxedEnvironment

        # Sandboxed: rendering operator-authored text must not be able to walk
        # the object graph of the scope values. No loader is created, so a
        # template cannot read files either.
        env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        try:
            rendered = env.from_string(source).render(**self.ctx.scope.snapshot())
        except Exception as exc:  # noqa: BLE001
            # Name the template error rather than the engine's: a missing
            # variable is the common case and "StrictUndefined" tells the
            # author nothing about which one.
            raise NodeError(f"模板渲染失败: {type(exc).__name__}: {exc}") from exc

        return {
            "text": rendered,
            "empty": not rendered.strip(),
            "length": len(rendered),
        }


class ToolNode(BaseNode):
    """Invoke a tool from the existing :class:`ToolRegistry`."""

    def execute(self) -> dict[str, Any]:
        name = str(self.param("name", "")).strip()
        if not name:
            raise NodeError("tool 需要 name")
        registry = self.ctx.service("tools")
        if not registry.has(name):
            raise NodeError(f"工具 {name!r} 未注册")
        arguments = json.loads(str(self.param("arguments", "{}") or "{}"))
        arguments = {k: _interpolate(str(v), self.ctx.scope.snapshot()) if isinstance(v, str) else v for k, v in arguments.items()}
        return {"tool": name, "result": registry.get(name).execute(json.dumps(arguments, ensure_ascii=False))}


# ───────────────────────────────────────────────────────── registration ──

def _p(name: str, kind: str = "text", label: str = "", default: Any = None, **kw: Any) -> ParamSpec:
    return ParamSpec(name=name, kind=kind, label=label or name, default=default, **kw)


def register_all(registry: NodeRegistry) -> NodeRegistry:
    """Register every built-in node type. Idempotent."""
    add = registry.register
    from .registry import NodeSpec

    def spec(type: str, label: str, category: str, handler: type[BaseNode], **kw: Any) -> None:
        add(NodeSpec(type=type, label=label, category=category, handler=handler, **kw))

    # control
    spec("start", "开始", "控制流", StartNode, doc="流程入口，绑定触发变量。")
    spec("end", "结束", "控制流", EndNode, params=[_p("status", "select", "结束状态", "ok", choices=["ok", "error"]), _p("message", "text", "提示")], doc="终止流程，可选择以失败结束。")
    spec("wait", "等待", "控制流", WaitNode, params=[_p("seconds", "number", "等待秒数", 5.0)], doc="暂停 N 秒，主循环用它替代硬编码 sleep。")
    spec("set_var", "设置变量", "控制流", SetVarNode, params=[_p("name", "text", "变量名", required=True), _p("operation", "select", "操作", "set", choices=["set", "increment"]), _p("by", "number", "增量", 1, help="operation=increment 时的步长"), _p("value", "text", "固定值"), _p("expression", "text", "表达式", help="留空则使用固定值；否则对作用域求值")], outputs=["value"], doc="把一个值绑定进运行作用域；increment 用于自增计数器。")
    spec("log", "记录日志", "控制流", LogNode, params=[_p("message", "text", "内容", "{{chat_name}} 已处理 {{message_count}} 条"), _p("level", "select", "级别", "info", choices=["info", "warn", "error"])], doc="向运行日志写一行。")
    spec("condition", "条件分支", "控制流", ConditionNode, params=[_p("expression", "text", "表达式", required=True, help="例：unreplied_count > 0")], outputs=["true", "false"], doc="按表达式走 true / false 端口。")

    # perception
    spec("capture", "窗口截图", "感知", CaptureNode, params=[], outputs=["image_path", "window_rect", "scale_factor"], requires=["screen_recording"], doc="截取微信窗口，不做解析。")
    spec("perceive", "智能感知", "感知", PerceiveNode, params=[], outputs=["chat_name", "messages", "chat_list", "screenshot_path", "message_count"], requires=["screen_recording", "accessibility"], doc="截图 + OCR/视觉模型 + 布局解析 + 消息抽取，一次完成。")
    spec("login_recovery", "登录恢复", "感知", LoginRecoveryNode, params=[], outputs=["recovered", "status"], requires=["accessibility"], doc="检测并处理微信登录界面。")
    spec("switch_chat", "切换会话", "感知", SwitchChatNode, params=[_p("strategy", "select", "策略", "unread", choices=["unread", "nickname", "back"]), _p("target_nickname", "text", "目标会话名")], outputs=["clicked", "nickname"], requires=["accessibility"], doc="点击左侧会话列表项，或返回列表。")

    # session
    spec("merge_session", "合并会话", "会话", MergeSessionNode, params=[_p("chat_name", "text", "会话名"), _p("mode", "select", "模式", "ocr", choices=["ocr", "weflow", "hybrid"])], outputs=["unreplied", "unreplied_count"], doc="把本轮消息并入会话并去重。")
    spec("get_unreplied", "取未回复", "会话", GetUnrepliedNode, params=[_p("chat_name", "text", "会话名")], outputs=["messages", "count"], doc="读取某会话待回复消息。")
    spec("mark_replied", "标记已回复", "会话", MarkRepliedNode, params=[_p("chat_name", "text", "会话名"), _p("mode", "select", "方式", "all", choices=["all", "text"]), _p("target_text", "text", "目标消息文本"), _p("reply_text", "text", "回复内容")], outputs=["marked"], doc="标记消息已回复，避免下轮重复。")
    spec("save_session", "保存会话", "会话", SaveSessionNode, params=[], outputs=["saved"], doc="会话与截图落盘。")

    # reply
    spec("reply_policy", "回复策略", "回复", ReplyPolicyNode, params=[], outputs=["allowed", "allowed_count", "filtered_count"], doc="按 ReplyPolicy 过滤自发消息。")
    spec("silent_gate", "静默门控", "回复", SilentGateNode, params=[_p("chat_name", "text", "会话名")], outputs=["whitelisted", "skip"], doc="非白名单会话走 skip 端口。")
    spec("generate_reply", "生成回复", "回复", GenerateReplyNode, params=[], outputs=["replies", "count"], doc="调用 ReplyGenerator 产出回复文本。")

    # action
    spec("send_message", "发送消息", "动作", SendMessageNode, params=[_p("text", "textarea", "内容", required=True, help="支持 {{变量}} 插值"), _p("chat_name", "text", "会话名"), _p("interval", "number", "多段间隔秒", 1.5)], outputs=["sent_count", "results"], requires=["accessibility"], doc="向当前会话输入并发送文本。")
    spec("click", "点击", "动作", ClickNode, params=[_p("element", "text", "元素名"), _p("x", "number", "X"), _p("y", "number", "Y")], outputs=["clicked"], requires=["accessibility"], doc="点击坐标或元素库中命名元素的中心。")

    # ── pointer gestures ──
    _locate_params = [
        _p("element", "text", "元素名"),
        _p("x", "number", "X"),
        _p("y", "number", "Y"),
    ]

    def _gesture(node_type: str, label: str, gesture: str, doc: str,
                 extra: list[ParamSpec] | None = None) -> None:
        handler = type(f"{node_type.title().replace('_', '')}Node", (PointerNode,), {"gesture": gesture})
        spec(node_type, label, "动作", handler,
             params=[*_locate_params, *(extra or [])],
             outputs=["x", "y", "gesture"], requires=["accessibility"], doc=doc)

    _gesture("hover", "悬停", "hover", "把指针移到目标位置，不点击。用于展开悬停菜单。")
    _gesture("right_click", "右键点击", "right_click", "在目标位置按下右键，通常弹出上下文菜单。")
    _gesture("double_click", "双击", "double_click", "在目标位置连续左键两次，常用于打开文件或进入目录。")
    _gesture("drag", "拖拽", "drag", "从 (x,y) 按下并拖到 (x2,y2) 后抬起。duration_ms 控制拖动时长，过短会被当成瞬移而忽略。",
             extra=[_p("x2", "number", "终点 X", required=True), _p("y2", "number", "终点 Y", required=True),
                    _p("duration_ms", "number", "拖动毫秒", 500)])
    _gesture("scroll", "滚动", "scroll", "在目标位置滚动滚轮。clicks 正数向上、负数向下，单位是「格」。",
             extra=[_p("clicks", "number", "格数", -3, help="正数向上滚，负数向下滚")])
    spec("type_keys", "输入按键", "动作", TypeKeysNode, params=[_p("text", "text", "文本"), _p("keys", "text", "按键", help="cliclick 键位串，如 'kp:return'")], outputs=["success"], requires=["accessibility"], doc="剪贴板粘贴文本，或发送按键。")
    spec("activate_app", "激活应用", "动作", ActivateAppNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["activated"], doc="把应用切到前台。")
    spec("window_rect", "读取窗口位置", "动作", WindowRectNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["x", "y", "width", "height", "found"], requires=["accessibility"], doc="读取窗口位置与大小。")
    spec("set_window_rect", "调整窗口", "动作", SetWindowRectNode, params=[_p("app", "text", "应用名", "WeChat"), _p("x", "number", "X", 0), _p("y", "number", "Y", 0), _p("width", "number", "宽", required=True), _p("height", "number", "高", required=True)], outputs=["moved", "x", "y", "width", "height"], requires=["accessibility"], doc="移动并调整窗口大小。")
    spec("minimize_window", "最小化窗口", "动作", MinimizeWindowNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["minimized"], requires=["accessibility"], doc="最小化主窗口。")
    spec("maximize_window", "最大化窗口", "动作", MaximizeWindowNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["maximized"], requires=["accessibility"], doc="最大化主窗口，填充可用工作区。")
    spec("close_window", "关闭窗口", "动作", CloseWindowNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["closed"], requires=["accessibility"], doc="关闭主窗口。不可撤销，请确认后再使用。")
    spec("set_clipboard", "写剪贴板", "动作", SetClipboardNode, params=[_p("text", "text", "内容")], outputs=["success"], doc="写入系统剪贴板。")

    # memory
    spec("memory_search", "记忆检索", "记忆", MemorySearchNode, params=[_p("query", "text", "查询", required=True), _p("top_k", "number", "条数", 5), _p("max_chars", "number", "最大字符", 4000)], outputs=["result", "found"], doc="检索本地记忆 wiki。")
    spec("memory_update", "记忆更新", "记忆", MemoryUpdateNode, params=[_p("chat_name", "text", "会话名"), _p("is_group", "bool", "是否群聊")], outputs=["queued"], doc="异步入队更新 wiki。")

    # record
    spec("record_tick", "记录 Tick", "记录", RecordTickNode, params=[_p("skip_reason", "text", "跳过原因"), _p("tick_id", "number", "Tick ID"), _p("should_reply", "bool", "是否回复"), _p("judge_score", "number", "Judge 分")], outputs=["recorded"], doc="写入 tick_log，供看板/评审/benchmark 读取。")
    spec("update_send_result", "回写发送结果", "记录", UpdateSendResultNode, params=[_p("tick_id", "number", "Tick ID")], outputs=["updated"], doc="把发送结果写回 tick_log。")

    # 多模态（路径 B）
    spec("locate", "定位", "双路径", LocateNode,
         params=[_p("description", "text", "视觉描述", help="给视觉模型的自然语言描述（路径B）"),
                 _p("element", "text", "元素名", help="元素库中的名字（路径A）；与 description 同时给时按节点 path 决定"),
                 _p("confidence", "number", "置信度阈值", 0.6)],
         outputs=["x", "y", "source", "confidence", "element_name"],
         doc="双路径定位：优先元素库(路径A)，失败可降级到视觉模型(路径B)。source 字段记录实际走了哪条。")
    spec("find_image", "图像模板定位", "双路径", FindImageNode,
         params=[_p("image", "text", "模板图路径", required=True),
                 _p("haystack", "text", "搜索图路径", help="留空则用本次运行最近一次 capture 的截图"),
                 _p("threshold", "number", "相似度阈值", 0.85)],
         outputs=["found", "x", "y", "width", "height", "score", "count", "reason"],
         doc="第三种定位方式：按像素模板在截图中查找位置。元素库和视觉模型都认不出的自绘控件用它。"),
    spec("find_color", "颜色定位", "双路径", FindColorNode,
         params=[_p("rgb", "text", "RGB 颜色", required=True, help="例：20,120,220"),
                 _p("haystack", "text", "搜索图路径"),
                 _p("threshold", "number", "色差容忍", 30)],
         outputs=["found", "x", "y", "width", "height", "count", "reason"],
         doc="按颜色查找区域中心。适合状态指示灯、纯色按钮。"),
    spec("assert_pixel", "像素断言", "双路径", AssertPixelNode,
         params=[_p("x", "number", "X", required=True), _p("y", "number", "Y", required=True),
                 _p("rgb", "text", "期望 RGB", help="留空则只读取并返回实际颜色"),
                 _p("tolerance", "number", "色差容忍", 12),
                 _p("haystack", "text", "图片路径")],
         outputs=["matched", "r", "g", "b", "actual", "reason"],
         doc="断言某点颜色是否符合预期。用于确认界面状态而不依赖文字。")
    spec("vlm_act", "多模态操作", "多模态", VisionActNode,
         params=[_p("goal", "text", "操作目标", required=True),
                 _p("context", "text", "补充上下文"),
                 _p("wait_seconds", "number", "wait 动作秒数", 1.0)],
         outputs=["decided", "performed", "x", "y"], requires=["screen_recording", "accessibility"],
         doc="让视觉模型决定并执行一步操作，用于无法捕获元素的界面。每次只做一步。")
    spec("vlm_describe", "多模态描述", "多模态", VisionDescribeNode,
         params=[_p("save_as_elements", "bool", "保存为元素", False),
                 _p("name_prefix", "text", "元素名前缀", "region")],
         outputs=["summary", "regions", "saved"], requires=["screen_recording"],
         doc="识别并命名当前界面的可交互区域，可一次性存为元素库条目。")
    spec("vlm_verify", "多模态校验", "多模态", VisionVerifyNode,
         params=[_p("claim", "text", "待验证说法", required=True)],
         outputs=["holds", "confidence", "evidence"], requires=["screen_recording"],
         doc="判断画面是否支持某个说法，用作昂贵步骤之前的前置断言。")

    # utility
    spec("http_request", "HTTP 请求", "通用", HttpRequestNode, params=[_p("url", "text", "URL", required=True), _p("method", "select", "方法", "GET", choices=["GET", "POST", "PUT", "DELETE"]), _p("headers", "textarea", "请求头", "{}"), _p("body", "textarea", "请求体"), _p("timeout", "number", "超时秒", 20)], outputs=["status", "body", "ok"], doc="发起 HTTP 请求。")
    spec("file", "文件读写", "通用", FileNode, params=[_p("mode", "select", "模式", "read", choices=["read", "write"]), _p("path", "text", "路径", required=True), _p("content", "textarea", "内容")], outputs=["content"], doc="在项目根目录内读写文本文件。")
    spec("template", "模板渲染", "通用", TemplateNode, params=[_p("template", "textarea", "模板", required=True, raw=True, help="Jinja2 语法；{{var}} 与快速插值一致，另支持 {% for %} / {% if %} / 过滤器")], outputs=["text", "empty", "length"], doc="用 Jinja2 渲染多行文本。沙箱环境，未定义变量直接报错。")
    spec("llm", "大模型调用", "通用", LLMNode, params=[_p("provider", "text", "Provider", help="留空用默认 provider；在设置页配置网关与密钥"), _p("prompt", "textarea", "提示词", required=True), _p("system", "textarea", "系统提示"), _p("model", "text", "模型", help="留空则用 provider 的模型"), _p("base_url", "text", "接口地址", help="留空则用 provider 的地址"), _p("temperature", "number", "温度", help="留空则不发送该参数"), _p("max_tokens", "number", "最大 token", 1024), _p("timeout", "number", "超时秒")], outputs=["text", "empty", "model"], doc="自由调用大模型。网关与密钥由设置页的 provider 管理，节点参数可覆盖。generate_reply 是本项目的回复专用节点。")
    spec("tool", "调用工具", "通用", ToolNode, params=[_p("name", "text", "工具名", required=True), _p("arguments", "textarea", "参数 JSON", "{}")], outputs=["result"], doc="调用 ToolRegistry 中已注册的工具。")

    return registry


# ────────────────────────────────────────────────────────────── helpers ──


def _interpolate(template: str, scope: dict[str, Any]) -> str:
    """Resolve ``{{...}}`` in a value that did not come from ``param()``.

    Declared parameters are interpolated by :meth:`BaseNode.param`, so this is
    only for values parsed out of a parameter — the argument map of a tool call,
    for instance — that therefore never passed through it. It delegates to the
    shared evaluator rather than keeping a second, weaker template engine
    around: this one accepted only dotted paths, silently left unknown names in
    place, and could not evaluate ``{{len(items)}}`` at all.
    """
    from .context import FlowScope
    from .expr import interpolate

    lookup = scope if isinstance(scope, FlowScope) else FlowScope(scope)
    return interpolate(str(template), lookup, strict=False)


def _slug(text: str) -> str:
    """ASCII-safe identifier fragment for generated element names."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(text).strip().lower())
    return (cleaned.strip("_") or "unnamed")[:40]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    sender_type = getattr(message, "sender_type", None)
    return {
        "text": getattr(message, "text", ""),
        "sender": getattr(message, "sender", ""),
        "sender_type": getattr(sender_type, "value", "other") if sender_type else "other",
        "chat_name": getattr(message, "chat_name", ""),
        "is_at_me": bool(getattr(message, "is_at_me", False)),
        "timestamp": getattr(message, "timestamp", None),
        "message_type": getattr(message, "message_type", "text"),
        "image_description": getattr(message, "image_description", ""),
        "quoted_text": getattr(message, "quoted_text", ""),
        "replied": bool(getattr(message, "replied", False)),
    }


def _chat_item_to_dict(item: Any) -> dict[str, Any]:
    rect = getattr(item, "rect", None)
    return {
        "nickname": getattr(item, "nickname", ""),
        "last_message_preview": getattr(item, "last_message_preview", ""),
        "unread_count": getattr(item, "unread_count", ""),
        "timestamp": getattr(item, "timestamp", ""),
        "rect": [rect.x, rect.y, rect.width, rect.height] if rect else [0, 0, 0, 0],
    }


def _dict_to_message(raw: dict[str, Any], fallback_pool: list[Any]) -> Any:
    """Rebuild a ChatMessage from a scope dict, reusing the original when present."""
    from src.models.base import ChatMessage, SenderType

    for candidate in fallback_pool:
        if not isinstance(candidate, dict) and getattr(candidate, "text", None) == raw.get("text") and getattr(candidate, "sender", None) == raw.get("sender"):
            return candidate
    try:
        sender_type = SenderType(raw.get("sender_type", "other"))
    except ValueError:
        sender_type = SenderType.OTHER
    return ChatMessage(
        text=raw.get("text", ""),
        sender=raw.get("sender", ""),
        sender_type=sender_type,
        chat_name=raw.get("chat_name", ""),
        is_at_me=bool(raw.get("is_at_me", False)),
        timestamp=raw.get("timestamp"),
        message_type=raw.get("message_type", "text"),
        image_description=raw.get("image_description", ""),
        quoted_text=raw.get("quoted_text", ""),
    )


def _should_reply(policy: Any, message: dict[str, Any]) -> bool:
    from src.models.base import ChatMessage, SenderType

    try:
        sender_type = SenderType(message.get("sender_type", "other"))
    except ValueError:
        sender_type = SenderType.OTHER
    probe = ChatMessage(
        text=message.get("text", ""),
        sender=message.get("sender", ""),
        sender_type=sender_type,
        chat_name=message.get("chat_name", ""),
        is_at_me=bool(message.get("is_at_me", False)),
    )
    try:
        return bool(policy.should_reply(probe, None))
    except TypeError:
        return bool(policy.should_reply(probe))
