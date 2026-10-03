"""Recording and element-picking routes for the desktop API.

Split out of :mod:`services.company_api.rpa_api` because these are the only routes
that interact with the operator's hands rather than with a stored flow: the
recorder watches what the user does, and the picker turns a rectangle they drew
into an element. Both are stateful in a way the rest of the API is not, and both
have a failure mode worth naming — a missing Accessibility permission — that the
client needs to surface as a fix rather than as an empty result.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.engine.picker import list_templates, pick_from_rect
from apps.engine.recorder import RecorderError, get_recorder
from apps.engine.recording_graph import actions_to_graph, actions_to_preview
from apps.engine.schema import NodeError, new_id, validate_flow
from apps.engine.store import get_store

router = APIRouter(prefix="/api", tags=["record"])


# ────────────────────────────────────────────────────────────── models ──

class PickRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    image_path: str
    x: int
    y: int
    width: int
    height: int
    flow_id: str | None = None
    save_template: bool = True
    min_confidence: float = 0.5


class BuildFlowRequest(BaseModel):
    name: str = "录制流程"
    description: str = ""


# ────────────────────────────────────────────────────────── recorder ──

@router.get("/record/status")
def record_status() -> dict[str, Any]:
    """Live state of the recorder, including a thread that died on its own.

    ``error`` is populated when the monitor thread exited: the UI shows that
    instead of a spinner that never stops.
    """
    recorder = get_recorder()
    return {
        "recording": recorder.recording,
        "error": recorder.error,
        "count": len(recorder.actions()),
    }


@router.post("/record/start")
def record_start() -> dict[str, Any]:
    """Begin capturing input. 409 when one is already running."""
    recorder = get_recorder()
    if recorder.recording:
        raise HTTPException(status_code=409, detail="已经在录制中")
    try:
        recorder.start()
    except RecorderError as exc:
        # 409 rather than 500: the request was well-formed, the machine is not
        # in a state that allows it, and the client has a concrete action to take.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"recording": True, "started_at": time.time()}


@router.post("/record/stop")
def record_stop(build: BuildFlowRequest | None = None) -> dict[str, Any]:
    """Stop and return the captured actions, plus a preview of the nodes.

    Does not create a flow. Turning a recording into a stored flow is a separate
    call so the user can look at what was captured, delete the parts they did not
    mean, and only then commit.
    """
    recorder = get_recorder()
    if not recorder.recording and not recorder.actions():
        raise HTTPException(status_code=409, detail="当前没有在录制")
    actions = recorder.stop()
    graph = actions_to_graph(actions, name=build.name if build else "录制流程",
                             description=build.description if build else "")
    return {
        "recording": False,
        "error": recorder.error,
        "count": len(actions),
        "actions": actions_to_preview(actions),
        "graph": graph,
        "warnings": _warnings_for(actions),
    }


@router.get("/record/actions")
def record_actions() -> dict[str, Any]:
    """What has been captured so far, without stopping."""
    recorder = get_recorder()
    actions = recorder.actions()
    return {"recording": recorder.recording, "count": len(actions),
            "actions": actions_to_preview(actions)}


@router.post("/record/discard")
def record_discard() -> dict[str, Any]:
    recorder = get_recorder()
    if recorder.recording:
        recorder.stop()
    recorder.clear()
    return {"recording": False, "count": 0}


@router.post("/record/commit")
def record_commit(payload: BuildFlowRequest) -> dict[str, Any]:
    """Persist the current recording as a new flow.

    Refuses an empty recording: a flow with only a start and an end is not
    something anybody means to keep.
    """
    recorder = get_recorder()
    actions = recorder.stop() if recorder.recording else recorder_actions()
    if not actions:
        raise HTTPException(status_code=400, detail="没有可提交的录制内容")

    store = get_store()
    flow_id = new_id("flow")
    graph = actions_to_graph(actions, name=payload.name, description=payload.description)
    issues = [i for i in validate_flow(graph, get_node_registry_types()) if i.severity == "error"]
    if issues:
        raise HTTPException(status_code=400, detail=f"生成的流程图不合法: {issues[0].message}")

    flow = store.save_flow(flow_id, payload.name or "录制流程", graph,
                           description=payload.description or graph.get("description", ""))
    recorder.clear()
    return {"flow_id": flow.id, "name": flow.name, "action_count": len(actions),
            "warnings": _warnings_for(actions)}


def get_node_registry_types() -> list[str]:
    from apps.engine.registry import get_node_registry

    return get_node_registry().types()


def recorder_actions() -> list[Any]:
    """Recorded actions as objects, not dicts, for the commit path."""
    from apps.engine.recorder import RecordedAction

    out: list[RecordedAction] = []
    for row in get_recorder().actions():
        out.append(
            RecordedAction(
                kind=row["kind"], at=row["at"], x=row["x"], y=row["y"],
                button=row["button"], clicks=row["clicks"], text=row["text"],
                scroll_delta=row["scroll_delta"], modifiers=row["modifiers"], app=row["app"],
            )
        )
    return out


def _warnings_for(actions: list[Any]) -> list[str]:
    """Things the author must know about before trusting the replay."""
    warnings: list[str] = []
    literal = sum(1 for a in actions if a.kind in ("click", "double_click", "scroll"))
    if literal:
        warnings.append(
            f"录制中有 {literal} 个动作使用固定坐标。窗口移动或改变大小后这些坐标会失效，"
            "建议在设计器中改为引用元素库。"
        )
    if any(a.kind == "type_keys" and a.modifiers for a in actions):
        warnings.append("录制中有带修饰键的输入，回放时使用的键盘布局必须与录制时一致。")
    if not actions:
        warnings.append("录制为空：如果刚才确实操作过，请检查「辅助功能」权限。")
    return warnings


# ─────────────────────────────────────────────────────────── picker ──

@router.get("/pick/screenshot")
def pick_screenshot(target: str = "wechat") -> dict[str, Any]:
    """A fresh screenshot for the picker overlay to draw a rectangle on.

    Reports the window origin alongside the image so the client can convert a
    drawn rectangle back to window coordinates, which is what the element store
    resolves against.
    """
    from apps.engine.context import FlowContext, FlowScope
    from apps.engine.elements import window_origin
    from apps.engine.services import register_default_services

    ctx = FlowContext(scope=FlowScope(), run_id="pick", flow_id="pick")
    register_default_services(ctx, dry_run=False)
    try:
        shot = ctx.service("capture").capture()
    except Exception as exc:  # noqa: BLE001 - surface the real reason
        raise HTTPException(status_code=409, detail=f"截图失败: {exc}") from exc

    origin = None
    try:
        origin = window_origin(ctx.service("automation"))
    except Exception:  # noqa: BLE001 - the screenshot is still usable without it
        origin = None
    return {
        "image_path": shot.image_path,
        "scale_factor": getattr(shot, "scale_factor", 1.0),
        "window_origin": list(origin) if origin else None,
    }


@router.post("/pick")
def pick(payload: PickRequest) -> dict[str, Any]:
    """Store an element from a rectangle drawn on a screenshot."""
    try:
        element = pick_from_rect(
            name=payload.name,
            image_path=payload.image_path,
            rect={"x": payload.x, "y": payload.y, "width": payload.width, "height": payload.height},
            flow_id=payload.flow_id,
            save_template=payload.save_template,
            min_confidence=payload.min_confidence,
        )
    except NodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    strategy = (element.get("meta") or {}).get("strategy", "fixed_rect")
    advice = {
        "ocr_anchor": "已用「" + (element.get("ocr_text") or "") + "」作为锚点，窗口移动后仍可定位。",
        "image_template": "已截取模板图用于图像匹配。",
        "fixed_rect": "该区域没有可识别的文字，已按固定坐标保存。窗口移动后需要重新拾取。",
    }.get(strategy, "")
    return {"element": element, "strategy": strategy, "advice": advice}


@router.get("/pick/templates")
def pick_templates() -> dict[str, Any]:
    return {"templates": list_templates()}
