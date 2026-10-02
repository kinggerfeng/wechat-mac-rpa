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
        raw = self.param("seconds", 5.0)
        # Accept {{interval}} so the main loop's cadence is a flow variable
        # rather than a value baked into the graph.
        seconds = float(_interpolate(str(raw), self.ctx.scope.snapshot()) or 0.0)
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

    ``expression`` is evaluated by :func:`eval` against ``ctx.scope.snapshot()``
    restricted to safe builtins. Flow authors are the operator of a local tool that
    already has full screen control, but an unrestricted ``eval`` here would also
    be reachable from a webhook payload, so the namespace is closed.
    """

    _SAFE_BUILTINS = {
        "abs": abs, "min": min, "max": max, "round": round, "int": int, "float": float,
        "str": str, "bool": bool, "len": len, "sorted": sorted, "sum": sum,
        "list": list, "dict": dict, "set": set, "any": any, "all": all, "range": range,
    }

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
        try:
            return eval(  # noqa: S307 - closed namespace, see _SAFE_BUILTINS
                str(expression),
                {"__builtins__": self._SAFE_BUILTINS},
                self.ctx.scope.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001
            raise NodeError(f"表达式求值失败: {exc}") from exc


class LogNode(BaseNode):
    """Write a line to the run trace. ``level`` is informational only."""

    def execute(self) -> dict[str, Any]:
        template = str(self.param("message", ""))
        text = _interpolate(template, self.ctx.scope.snapshot())
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

    ``expression`` accepts the same grammar as edge conditions (``name op literal``)
    and, when it does not match that grammar, is evaluated as a Python expression
    against the run scope. Ports: ``true`` / ``false``.
    """

    def execute(self) -> dict[str, Any]:
        from .context import evaluate_condition
        from .schema import CONDITION_RE, FlowError

        expression = str(self.param("expression", "")).strip()
        if not expression:
            raise NodeError("condition 需要 expression 参数")
        if CONDITION_RE.match(expression):
            try:
                value = evaluate_condition(expression, self.ctx.scope)
            except FlowError:
                value = bool(self._eval_python(expression))
        else:
            value = bool(self._eval_python(expression))
        # The executor routes on this key: it is how a node chooses a port without
        # raising, so the span is recorded as a normal success.
        return {"__branch__": "true" if value else "false", "value": value, "expression": expression}

    def _eval_python(self, expression: str) -> Any:
        try:
            return eval(expression, {"__builtins__": {}}, self.ctx.scope.snapshot())  # noqa: S307
        except Exception as exc:  # noqa: BLE001
            raise NodeError(f"条件表达式求值失败: {exc}") from exc


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
        text = _interpolate(str(self.param("text", "")), self.ctx.scope.snapshot())
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


class TypeKeysNode(BaseNode):
    """Send keystrokes through cliclick, or paste ``text`` via the clipboard."""

    def execute(self) -> dict[str, Any]:
        automation = self.ctx.service("automation")
        text = _interpolate(str(self.param("text", "")), self.ctx.scope.snapshot())
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


class SetClipboardNode(BaseNode):
    """Put text on the system clipboard."""

    def execute(self) -> dict[str, Any]:
        text = _interpolate(str(self.param("text", "")), self.ctx.scope.snapshot())
        return {"success": bool(self.ctx.service("automation").set_clipboard_text(text)), "length": len(text)}


# ───────────────────────────────────────────────────────────── memory ──

class MemorySearchNode(BaseNode):
    """Search the local memory wiki."""

    def execute(self) -> dict[str, Any]:
        query = _interpolate(str(self.param("query", "")), self.ctx.scope.snapshot())
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
                automation.send_keys("cmd+v")
            outcome.update({"performed": True, "typed": decision.text[:120]})
        elif decision.action == "hotkey":
            ok = bool(automation.send_keys(decision.keys))
            outcome.update({"performed": ok, "keys": decision.keys})
        elif decision.action == "scroll":
            outcome.update({"performed": False, "note": f"scroll {decision.direction} 未自动执行", "scroll": decision.direction})
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
        url = _interpolate(str(self.param("url", "")), self.ctx.scope.snapshot())
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
        raw_path = _interpolate(str(self.param("path", "")), self.ctx.scope.snapshot())
        if not raw_path:
            raise NodeError("file 需要 path")
        target = (PROJECT_ROOT / raw_path).resolve()
        if not str(target).startswith(str(PROJECT_ROOT.resolve())):
            raise NodeError("路径越出项目根目录，已拒绝")
        if mode == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = _interpolate(str(self.param("content", "")), self.ctx.scope.snapshot())
            target.write_text(content, encoding="utf-8")
            return {"written": True, "path": str(target.relative_to(PROJECT_ROOT)), "bytes": len(content.encode("utf-8"))}
        if not target.is_file():
            return {"read": False, "path": str(raw_path), "reason": "file not found", "content": ""}
        return {"read": True, "path": str(target.relative_to(PROJECT_ROOT)), "content": target.read_text(encoding="utf-8", errors="replace")[:20000]}


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
    spec("type_keys", "输入按键", "动作", TypeKeysNode, params=[_p("text", "text", "文本"), _p("keys", "text", "按键", help="cliclick 键位串，如 'kp:return'")], outputs=["success"], requires=["accessibility"], doc="剪贴板粘贴文本，或发送按键。")
    spec("activate_app", "激活应用", "动作", ActivateAppNode, params=[_p("app", "text", "应用名", "WeChat")], outputs=["activated"], doc="把应用切到前台。")
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
    spec("tool", "调用工具", "通用", ToolNode, params=[_p("name", "text", "工具名", required=True), _p("arguments", "textarea", "参数 JSON", "{}")], outputs=["result"], doc="调用 ToolRegistry 中已注册的工具。")

    return registry


# ────────────────────────────────────────────────────────────── helpers ──

_TEMPLATE_RE = None


def _interpolate(template: str, scope: dict[str, Any]) -> str:
    """Replace ``{{path.to.value}}`` with the scope value; unknown names stay put."""
    import re

    global _TEMPLATE_RE
    if _TEMPLATE_RE is None:
        _TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.\[\]]*)\s*\}\}")

    from .context import FlowScope

    lookup = scope if isinstance(scope, FlowScope) else FlowScope(scope)
    return _TEMPLATE_RE.sub(lambda m: str(_stringify(lookup.get(m.group(1)))), template)


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
