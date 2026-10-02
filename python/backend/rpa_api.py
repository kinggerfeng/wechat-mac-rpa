"""Flow-engine routes for the desktop API.

Kept out of ``app.py`` so that file stays a thin process supervisor. Everything
here is the surface the Tauri canvas, the CLI-equivalent tooling, and external
triggers (webhook / scheduled) all share — one implementation, three callers.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

# src.flow lives at the repository root, not next to this module.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.flow.registry import get_node_registry
from src.flow.scheduler import CronError, CronSchedule, get_scheduler
from src.flow.schema import PATH_CHOICES, Flow, FlowError, new_id, validate_flow
from src.flow.store import get_store
from src.flow.strategy import get_target_registry

router = APIRouter(prefix="/api", tags=["flow"])


def _manager():
    from src.flow.runner import get_run_manager

    return get_run_manager()


def _require_flow(flow_id: str) -> Flow:
    flow = get_store().get_flow(flow_id)
    if flow is None:
        flow = get_store().get_flow_by_name(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"流程 {flow_id!r} 不存在")
    return flow


def _graph_or_400(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="graph 必须是对象")
    return raw


# ──────────────────────────────────────────────────────────── node palette ──

@router.get("/flow/nodes")
def node_catalogue() -> dict[str, Any]:
    """Everything the canvas needs to draw a palette and render a node body."""
    registry = get_node_registry()
    return {
        "categories": registry.catalogue(),
        "path_choices": list(PATH_CHOICES),
        "total": len(registry.types()),
    }


@router.get("/flow/targets")
def list_targets() -> dict[str, Any]:
    return {"targets": get_target_registry().list()}


@router.post("/flow/targets")
def upsert_target(payload: dict[str, Any]) -> dict[str, Any]:
    """Register an application so flows can target it by name."""
    from src.flow.strategy import LocateMode, Target

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    try:
        mode = LocateMode(str(payload.get("mode") or "auto"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"非法 mode: {exc}") from exc
    try:
        target = Target(
            name=name,
            kind=str(payload.get("kind") or "desktop"),
            match=str(payload.get("match") or ""),
            mode=mode,
            window_titles=list(payload.get("window_titles") or []),
            url_pattern=str(payload.get("url_pattern") or ""),
            description=str(payload.get("description") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_target_registry().register(target).to_dict()


# ──────────────────────────────────────────────────────────────── flows ──

@router.get("/flows")
def list_flows() -> dict[str, Any]:
    manager = _manager()
    rows = get_store().list_flows()
    for row in rows:
        row["running"] = manager.is_running(row["id"])
    return {"flows": rows}


@router.post("/flows")
def create_flow(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    graph = _graph_or_400(payload.get("graph") or {"version": 1, "entry": "", "nodes": [], "edges": []})
    if get_store().get_flow_by_name(name) is not None:
        raise HTTPException(status_code=409, detail=f"已存在同名流程 {name!r}")
    flow_id = str(payload.get("id") or new_id("flow"))
    return get_store().save_flow(
        flow_id, name, graph, description=str(payload.get("description") or "")
    ).to_dict()


@router.get("/flows/{flow_id}")
def get_flow(flow_id: str) -> dict[str, Any]:
    flow = _require_flow(flow_id)
    data = flow.to_dict()
    data["issues"] = [i.to_dict() for i in validate_flow(flow.graph, get_node_registry().types())]
    return data


@router.put("/flows/{flow_id}")
def save_flow(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save a graph. ``strict=false`` (default) saves a graph with warnings so an
    operator can work on it; errors are still rejected because the executor
    cannot run them."""
    flow = _require_flow(flow_id)
    graph = _graph_or_400(payload.get("graph"))
    if graph.get("version") != 1:
        raise HTTPException(status_code=400, detail=f"不支持的流程版本 {graph.get('version')!r}")
    issues = validate_flow(graph, get_node_registry().types())
    errors = [i for i in issues if i.severity == "error"]
    if errors and payload.get("strict", False):
        raise HTTPException(status_code=422, detail={"errors": [i.to_dict() for i in errors]})
    name = str(payload.get("name") or flow.name)
    saved = get_store().save_flow(
        flow.id,
        name,
        graph,
        description=str(payload.get("description", flow.description) or ""),
        is_active=payload.get("is_active"),
    )
    data = saved.to_dict()
    data["issues"] = [i.to_dict() for i in issues]
    return data


@router.post("/flows/{flow_id}/validate")
def validate_only(flow_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a candidate graph without saving it — what the canvas calls on
    every edit, so an operator sees the problem before it becomes a run failure."""
    graph = payload.get("graph") if payload and payload.get("graph") else _require_flow(flow_id).graph
    graph = _graph_or_400(graph)
    issues = [i.to_dict() for i in validate_flow(graph, get_node_registry().types())]
    return {
        "ok": not [i for i in issues if i["severity"] == "error"],
        "issues": issues,
        "errors": [i for i in issues if i["severity"] == "error"],
        "warnings": [i for i in issues if i["severity"] == "warning"],
    }


@router.post("/flows/{flow_id}/duplicate")
def duplicate_flow(flow_id: str) -> dict[str, Any]:
    flow = _require_flow(flow_id)
    name = f"{flow.name} 副本"
    suffix = 2
    while get_store().get_flow_by_name(name) is not None:
        name = f"{flow.name} 副本{suffix}"
        suffix += 1
    return get_store().save_flow(new_id("flow"), name, flow.graph, description=flow.description).to_dict()


@router.delete("/flows/{flow_id}")
def delete_flow(flow_id: str) -> dict[str, Any]:
    if _manager().is_running(flow_id):
        raise HTTPException(status_code=409, detail="流程正在运行，请先停止")
    if not get_store().delete_flow(flow_id):
        raise HTTPException(status_code=404, detail=f"流程 {flow_id!r} 不存在")
    return {"deleted": flow_id}


# ───────────────────────────────────────────────────────────────── runs ──

@router.post("/flows/{flow_id}/run")
def start_run(flow_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a run and return immediately with its id.

    Refuses when the graph has errors — starting a run that is certain to fail
    just produces a confusing trace. Pass ``force=true`` to override.
    """
    payload = payload or {}
    flow = _require_flow(flow_id)
    variables = payload.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    dry_run = bool(payload.get("dry_run", False))

    if not dry_run and not payload.get("force"):
        issues = [i for i in validate_flow(flow.graph, get_node_registry().types()) if i.severity == "error"]
        if issues:
            raise HTTPException(
                status_code=422,
                detail={"message": "流程校验未通过", "errors": [i.to_dict() for i in issues]},
            )

    outcome = _manager().submit(
        flow,
        trigger_type=str(payload.get("trigger_type") or "manual"),
        trigger_ref=str(payload.get("trigger_ref") or "api"),
        variables=variables,
        dry_run=dry_run,
    )
    if not outcome.get("accepted"):
        raise HTTPException(status_code=409, detail=outcome.get("message") or "无法启动")
    return outcome


@router.get("/runs")
def list_runs(flow_id: str | None = None, status: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"runs": get_store().list_runs(flow_id=flow_id, status=status, limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = get_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id!r} 不存在")
    run["span_count"] = len(get_store().list_spans(run_id, limit=10000))
    return run


@router.get("/runs/{run_id}/spans")
def get_spans(run_id: str, limit: int = Query(2000, ge=1, le=10000)) -> dict[str, Any]:
    if get_store().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id!r} 不存在")
    return {"spans": get_store().list_spans(run_id, limit=limit)}


@router.post("/runs/{run_id}/abort")
def abort_run(run_id: str) -> dict[str, Any]:
    for flow_id, active in list(_manager()._running.items()):  # noqa: SLF001 - no public reverse lookup
        if active == run_id:
            return _manager().abort(flow_id)
    return {"aborted": False, "reason": "not_running"}


@router.post("/runs/abort-all")
def abort_everything() -> dict[str, Any]:
    return {"aborted": _manager().abort_all()}


@router.post("/runs/prune")
def prune_runs(keep: int = Query(500, ge=10, le=100000)) -> dict[str, Any]:
    return {"removed": get_store().prune_runs(keep)}


# ────────────────────────────────────────────────────────────────── SSE ──

@router.get("/stream")
async def stream(request: Request, run_id: str | None = None) -> StreamingResponse:
    """Live trace.

    Each node emits twice — ``phase: started`` the moment it begins, ``phase:
    finished`` when it settles — so a run that is blocked on a permission prompt
    shows the node it is stuck on rather than an empty screen.

    A heartbeat comment every 15s keeps proxies and the Tauri webview from
    timing the connection out during a long quiet stretch.
    """
    manager = _manager()
    subscriber = manager.subscribe()
    backlog = manager.backfill(run_id) if run_id else []

    async def generator():
        try:
            for event in backlog:
                if await request.is_disconnected():
                    return
                yield _sse(event)
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = subscriber.queue.get_nowait()
                except Exception:  # noqa: BLE001 - queue.Empty
                    await asyncio.sleep(0.15)
                    if subscriber.dropped:
                        yield _sse({"type": "dropped", "count": subscriber.dropped})
                        subscriber.dropped = 0
                    continue
                if run_id and event.get("run_id") != run_id:
                    continue
                yield _sse(event)
        finally:
            manager.unsubscribe(subscriber)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


# ───────────────────────────────────────────────────────────── elements ──

@router.get("/elements")
def list_elements(flow_id: str | None = None) -> dict[str, Any]:
    from src.flow.elements import describe_element

    return {"elements": [describe_element(e) for e in get_store().list_elements(flow_id)]}


@router.post("/elements")
def save_element(payload: dict[str, Any]) -> dict[str, Any]:
    from src.flow.elements import capture_element

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    rect = payload.get("rect") or {}
    if not isinstance(rect, dict):
        raise HTTPException(status_code=400, detail="rect 必须是对象")
    try:
        element = capture_element(
            name=name,
            kind=str(payload.get("kind") or "rect"),
            rect=rect,
            flow_id=payload.get("flow_id"),
            ocr_text=str(payload.get("ocr_text") or ""),
            image_path=payload.get("image_path"),
            anchor=payload.get("anchor") or {},
            meta=payload.get("meta") or {},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from src.flow.elements import describe_element

    return describe_element(element)


@router.delete("/elements/{element_id}")
def delete_element(element_id: str) -> dict[str, Any]:
    if not get_store().delete_element(element_id):
        raise HTTPException(status_code=404, detail="元素不存在")
    return {"deleted": element_id}


@router.post("/elements/{name}/resolve")
def resolve_element(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dry-resolve a named element and report what it points at.

    This is how an operator checks an element after moving or resizing the
    window: no click happens, it just reports the point and which anchor won.
    """
    from src.flow.elements import locate_element
    from src.flow.context import FlowContext, FlowScope
    from src.flow.services import register_default_services

    payload = payload or {}
    ctx = FlowContext(scope=FlowScope(payload.get("variables") or {}), run_id="probe", flow_id="probe")
    register_default_services(ctx, dry_run=True)
    try:
        located = locate_element(ctx, name)
    except Exception as exc:  # noqa: BLE001
        return {"resolved": False, "name": name, "error": f"{type(exc).__name__}: {exc}"}
    if located is None:
        return {"resolved": False, "name": name, "error": "元素库中不存在该名称"}
    return {"resolved": True, **located.to_dict()}


# ─────────────────────────────────────────────────────────── permissions ──

@router.get("/permissions")
def get_permissions(deep: bool = Query(False, description="true 会触发系统授权弹窗，仅权限页使用")) -> dict[str, Any]:
    from src.flow.permissions import check_all

    return check_all(deep=deep)


@router.post("/permissions/{key}/prompt")
def prompt_permission(key: str) -> dict[str, Any]:
    from src.flow.permissions import request_prompt

    result = request_prompt(key)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/permissions/{key}/open")
def open_permission_pane(key: str) -> dict[str, Any]:
    from src.flow.permissions import open_pane

    result = open_pane(key)
    if not result.get("opened"):
        raise HTTPException(status_code=400, detail=result.get("error") or "无法打开设置面板")
    return result


@router.post("/permissions/vision/reset")
def reset_vision_client() -> dict[str, Any]:
    """Drop the cached VLM client so a key change takes effect without a restart."""
    from src.flow.vision import reset_vision_client

    reset_vision_client()
    return {"reset": True}


# ───────────────────────────────────────────────────────────── schedules ──

@router.get("/schedules")
def list_schedules(enabled_only: bool = False) -> dict[str, Any]:
    scheduler = get_scheduler()
    rows = get_store().list_schedules(enabled_only=enabled_only)
    for row in rows:
        try:
            row["cron_preview"] = scheduler.describe(row["cron"]).get("next_runs", [])
        except Exception:  # noqa: BLE001
            row["cron_preview"] = []
    return {"schedules": rows, "running": scheduler.running, "stats": scheduler.stats.to_dict()}


@router.post("/schedules")
def upsert_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    cron = str(payload.get("cron") or "").strip()
    flow_id = str(payload.get("flow_id") or "").strip()
    if not name or not cron or not flow_id:
        raise HTTPException(status_code=400, detail="name / cron / flow_id 均为必填")
    if get_store().get_flow(flow_id) is None:
        raise HTTPException(status_code=404, detail=f"流程 {flow_id!r} 不存在")
    try:
        CronSchedule.parse(cron)
    except CronError as exc:
        raise HTTPException(status_code=400, detail=f"cron 表达式非法: {exc}")
    return get_store().save_schedule({
        "id": payload.get("id"),
        "name": name,
        "flow_id": flow_id,
        "cron": cron,
        "enabled": payload.get("enabled", True),
    })


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str) -> dict[str, Any]:
    if not get_store().delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="计划不存在")
    return {"deleted": schedule_id}


# ── LLM providers ───────────────────────────────────────────────────
# Provider rows live in rpa.db so switching gateway is a settings change, not
# a file edit plus a restart. The stored key is never returned: listings carry
# a mask, and only the connectivity test — which runs in-process — sees it.


def _provider_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "name": str(raw.get("name") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": raw.get("api_key") or "",
        "model": str(raw.get("model") or "").strip(),
        "temperature": raw.get("temperature"),
        "max_tokens": raw.get("max_tokens"),
        "timeout": raw.get("timeout"),
        "is_default": int(bool(raw.get("is_default", False))),
        "enabled": int(bool(raw.get("enabled", True))),
        "note": str(raw.get("note") or ""),
    }


@router.get("/llm/providers")
def list_llm_providers() -> dict[str, Any]:
    providers = get_store().list_providers()
    return {
        "providers": providers,
        # The settings page needs to say "no provider configured yet" without
        # having to infer it from an empty list plus a separate default lookup.
        "has_default": any(p.get("is_default") for p in providers),
    }


@router.post("/llm/providers")
def upsert_llm_provider(payload: dict[str, Any]) -> dict[str, Any]:
    body = _provider_payload(payload)
    if not body["name"]:
        raise HTTPException(status_code=400, detail="name 必填")
    if not body["base_url"]:
        raise HTTPException(status_code=400, detail="base_url 必填")
    if not body["base_url"].startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url 必须以 http:// 或 https:// 开头")
    saved = get_store().save_provider(body)
    if not saved:
        raise HTTPException(status_code=500, detail="保存失败")
    return saved


@router.delete("/llm/providers/{provider_id}")
def delete_llm_provider(provider_id: str) -> dict[str, Any]:
    if not get_store().delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return {"deleted": provider_id}


@router.post("/llm/providers/{provider_id}/test")
def test_llm_provider(provider_id: str) -> dict[str, Any]:
    """Send a real one-token request so the settings page proves the row.

    Saves a real round trip on a typo'd base_url, which otherwise only shows up
    as a failed flow run minutes later with no link to the provider that caused
    it. The outcome is recorded on the row so the list can show health without
    every reload re-probing the gateway.
    """
    store = get_store()
    provider = store.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider 不存在")

    from src.llm.openclaw_client import OpenClawClient

    try:
        client = OpenClawClient(
            base_url=provider["base_url"],
            api_key=provider.get("api_key") or "",
            model=provider.get("model") or "",
        )
        reply = client.chat(
            [{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=32,
        )
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        store.mark_provider_result(provider_id, ok=False, error=message)
        # 502, not 500: the gateway is upstream and the row is fine.
        raise HTTPException(status_code=502, detail=message)

    text = reply if isinstance(reply, str) else ""
    store.mark_provider_result(provider_id, ok=True)
    return {"ok": True, "reply": text[:200], "model": client.model}


@router.post("/schedules/cron/preview")
def preview_cron(payload: dict[str, Any]) -> dict[str, Any]:
    return get_scheduler().describe(str(payload.get("cron") or ""))


@router.post("/scheduler/tick")
def scheduler_tick() -> dict[str, Any]:
    """Force one evaluation pass. Used by the schedule editor to see what a cron
    would do right now instead of waiting for the next real tick."""
    return {"fired": get_scheduler().tick()}


# ─────────────────────────────────────────────────────────────── webhook ──

@router.post("/webhook/{token}")
async def webhook(token: str, request: Request) -> dict[str, Any]:
    """Trigger a flow from outside the app.

    A shared secret in the path rather than a header, because the callers are
    mostly chatops platforms that cannot be configured to send custom headers.
    The token is compared in constant time and maps to a flow by name, so
    revoking a trigger is a rename rather than a redeploy.
    """
    expected = _webhook_token()
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="未知的 webhook")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    flow_name = str(payload.pop("flow") or payload.pop("flow_name") or "").strip()
    if not flow_name:
        raise HTTPException(status_code=400, detail="payload 需要 flow 指定流程名")

    variables = payload.pop("variables", None)
    if not isinstance(variables, dict):
        variables = payload
    variables.pop("flow", None)
    variables.pop("flow_name", None)
    variables.pop("token", None)

    flow = get_store().get_flow_by_name(flow_name) or get_store().get_flow(flow_name)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"流程 {flow_name!r} 不存在")

    dry_run = bool(payload.pop("dry_run", False))
    outcome = _manager().submit(
        flow,
        trigger_type="webhook",
        trigger_ref=f"webhook:{token[:6]}",
        variables=variables,
        dry_run=dry_run,
    )
    if not outcome.get("accepted"):
        raise HTTPException(status_code=409, detail=outcome.get("message") or "无法启动")
    return outcome


def _webhook_token() -> str:
    import os

    return os.environ.get("RPA_WEBHOOK_TOKEN", "")


@router.get("/webhook/info")
def webhook_info() -> dict[str, Any]:
    token = _webhook_token()
    return {
        "configured": bool(token),
        "path": f"/api/webhook/{token}" if token else None,
        "hint": "设置环境变量 RPA_WEBHOOK_TOKEN 后生效" if not token else "妥善保管该地址，等同于启动权限",
    }
