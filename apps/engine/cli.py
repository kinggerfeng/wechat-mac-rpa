#!/usr/bin/env python3
"""Flow CLI — the external trigger surface.

An RPA that can only be started by clicking a button in its own window is not
automatable. This is the entry point other systems call:

    python -m apps.engine.cli run --flow default --once
    python -m apps.engine.cli run --flow default --var tick_id=7 --wait
    python -m apps.engine.cli list
    python -m apps.engine.cli validate --flow default
    python -m apps.engine.cli export --flow default --out flows/bot.yaml
    python -m apps.engine.cli permission

Exit codes are meaningful so a shell scheduler or CI step can branch on them:
  0 success · 1 run failed · 2 bad usage / flow not found · 3 permission missing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.engine.registry import get_node_registry  # noqa: E402
from apps.engine.schema import validate_flow  # noqa: E402
from apps.engine.seed import SEED_FLOW_ID, seed  # noqa: E402
from apps.engine.store import get_store  # noqa: E402

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_PERMISSION = 3


def _parse_var(raw: str) -> tuple[str, Any]:
    key, _, value = raw.partition("=")
    if not key:
        raise argparse.ArgumentTypeError(f"变量格式应为 key=value，收到 {raw!r}")
    try:
        return key, json.loads(value)
    except ValueError:
        return key, value


def _resolve_flow(store: Any, reference: str) -> Any:
    flow = store.get_flow(reference)
    if flow is None:
        flow = store.get_flow_by_name(reference)
    if flow is None and reference in ("default", "seed"):
        flow = store.get_flow(SEED_FLOW_ID)
    if flow is None and store.list_flows():
        flow = store.get_flow_by_name(store.list_flows()[0]["name"])
    return flow


def _cmd_list(args: argparse.Namespace) -> int:
    store = get_store()
    seed(store)
    rows = store.list_flows()
    if not rows:
        print("没有流程。用 `run` 子命令会创建默认流程。")
        return EXIT_OK
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{'ID':<28} {'名称':<26} {'运行':<5} {'最近状态':<10} 更新时间")
    for row in rows:
        print(f"{row['id']:<28} {row['name']:<26} {row['run_count']:<5} {row['last_status'] or '-':<10} {row['updated_at']}")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    store = get_store()
    seed(store)
    flow = _resolve_flow(store, args.flow)
    if flow is None:
        print(f"找不到流程 {args.flow!r}", file=sys.stderr)
        return EXIT_USAGE
    issues = validate_flow(flow.graph, get_node_registry().types())
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    if args.json:
        print(json.dumps([i.to_dict() for i in issues], ensure_ascii=False, indent=2))
    else:
        print(f"流程 {flow.name}：{len(flow.nodes)} 节点 / {len(flow.edges)} 连线")
        print(f"  错误 {len(errors)} · 警告 {len(warnings)}")
        for issue in issues:
            print(f"  [{issue.severity}] {issue.code}: {issue.message}")
    return EXIT_USAGE if errors else EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    store = get_store()
    seed(store)
    flow = _resolve_flow(store, args.flow)
    if flow is None:
        print(f"找不到流程 {args.flow!r}", file=sys.stderr)
        return EXIT_USAGE

    issues = [i for i in validate_flow(flow.graph, get_node_registry().types()) if i.severity == "error"]
    if issues and not args.force:
        print(f"流程校验未通过，先修掉 {len(issues)} 个错误（或用 --force 强制运行）：", file=sys.stderr)
        for issue in issues[:10]:
            print(f"  {issue.code}: {issue.message}", file=sys.stderr)
        return EXIT_USAGE

    if not args.skip_preflight:
        from apps.engine.permissions import preflight

        check = preflight()
        if not check["ok"]:
            print("权限不足，拒绝启动：", file=sys.stderr)
            print(f"  屏幕录制: {check['screen_recording']['detail']}", file=sys.stderr)
            print(f"  辅助功能: {check['accessibility']['detail']}", file=sys.stderr)
            print("先在系统设置里授权，或用 --skip-preflight 强制运行。", file=sys.stderr)
            return EXIT_NO_PERMISSION

    from apps.engine.runner import get_run_manager

    manager = get_run_manager()
    outcome = manager.submit(
        flow,
        trigger_type="cli",
        trigger_ref=args.trigger_ref,
        variables=args.variables,
        dry_run=args.dry_run,
        wait=args.wait or args.once,
    )
    if not outcome.get("accepted"):
        print(f"未接受：{outcome.get('message')}", file=sys.stderr)
        return EXIT_RUN_FAILED

    run_id = outcome["run_id"]
    print(f"运行已启动 {run_id}", file=sys.stderr)
    if not (args.wait or args.once):
        return EXIT_OK

    run = None
    for _ in range(args.timeout * 10):
        run = store.get_run(run_id)
        if run and run.get("status") != "running":
            break
        time.sleep(0.1)

    if run is None or run.get("status") == "running":
        print(f"运行 {run_id} 未在 {args.timeout}s 内结束", file=sys.stderr)
        return EXIT_RUN_FAILED
    print(json.dumps({k: run.get(k) for k in ("id", "status", "steps", "error", "started_at", "ended_at")},
                     ensure_ascii=False, indent=2))
    return EXIT_OK if run.get("status") == "ok" else EXIT_RUN_FAILED


def _cmd_export(args: argparse.Namespace) -> int:
    store = get_store()
    seed(store)
    flow = _resolve_flow(store, args.flow)
    if flow is None:
        print(f"找不到流程 {args.flow!r}", file=sys.stderr)
        return EXIT_USAGE
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": flow.id, "name": flow.name, "description": flow.description, **flow.graph}
    if out.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            out.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except ImportError:
            out = out.with_suffix(".json")
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"未安装 PyYAML，已改写为 {out}", file=sys.stderr)
    else:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出到 {out}")
    return EXIT_OK


def _cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"找不到文件 {path}", file=sys.stderr)
        return EXIT_USAGE
    text = path.read_text(encoding="utf-8")
    try:
        graph = json.loads(text)
    except ValueError:
        try:
            import yaml  # type: ignore

            graph = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001
            print(f"无法解析 {path}：{exc}", file=sys.stderr)
            return EXIT_USAGE
    graph.pop("id", None)
    graph.pop("description", None)
    issues = [i for i in validate_flow(graph, get_node_registry().types()) if i.severity == "error"]
    if issues and not args.force:
        print(f"校验未通过（{len(issues)} 个错误），用 --force 忽略：", file=sys.stderr)
        for issue in issues[:10]:
            print(f"  {issue.code}: {issue.message}", file=sys.stderr)
        return EXIT_USAGE
    flow_id = args.id or f"flow_{int(time.time())}"
    store = get_store()
    store.save_flow(flow_id, args.name or path.stem, graph, description=graph.get("description", ""))
    print(f"已导入流程 {flow_id}")
    return EXIT_OK


def _cmd_permission(args: argparse.Namespace) -> int:
    from apps.engine.permissions import check_all

    result = check_all()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["summary"])
        for permission in result["permissions"]:
            mark = "✓" if permission["granted"] else "✗"
            print(f"  {mark} {permission['title']}：{permission['detail']}")
            if not permission["granted"]:
                print(f"      授权方式：{permission['fix']}")
        print(f"  微信状态：{result['wechat_running']}")
    return EXIT_OK if result["ok"] else EXIT_NO_PERMISSION


def _cmd_runs(args: argparse.Namespace) -> int:
    store = get_store()
    rows = store.list_runs(limit=args.limit, status=args.status)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    for row in rows:
        print(f"{row['id']}  {row['flow_name'] or row['flow_id']}  {row['status']:<8} steps={row['steps']:<4} {(row['error'] or '')[:60]}")
    return EXIT_OK


def _cmd_trace(args: argparse.Namespace) -> int:
    store = get_store()
    spans = store.list_spans(args.run_id)
    if not spans:
        print(f"运行 {args.run_id} 没有轨迹", file=sys.stderr)
        return EXIT_USAGE
    for span in spans:
        duration = f"{span['duration_ms']}ms" if span["duration_ms"] is not None else "-"
        line = f"{span['id']:>5} {span['status']:<7} {span['node_type']:<16} {span['name'][:22]:<22} {duration:>8}"
        if span["error"]:
            line += f"  {span['error'][:70]}"
        print(line)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.engine.cli", description="WeChat RPA 流程引擎命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行一个流程")
    run.add_argument("--flow", default="default", help="流程 ID 或名称")
    run.add_argument("--var", dest="variables", action="append", type=_parse_var, default=[], metavar="KEY=VALUE")
    run.add_argument("--once", action="store_true", help="跑完即退出（等同 --wait）")
    run.add_argument("--wait", action="store_true", help="等待运行结束并打印结果")
    run.add_argument("--dry-run", action="store_true", help="不触碰屏幕，用空实现执行")
    run.add_argument("--timeout", type=int, default=300, help="等待结束的最长秒数")
    run.add_argument("--force", action="store_true", help="校验失败也运行")
    run.add_argument("--skip-preflight", action="store_true", help="跳过权限预检")
    run.add_argument("--trigger-ref", default="cli", help="触发来源标识，写入运行记录")
    run.set_defaults(func=_cmd_run)

    listing = sub.add_parser("list", help="列出流程")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_cmd_list)

    validate = sub.add_parser("validate", help="校验流程图")
    validate.add_argument("--flow", default="default")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=_cmd_validate)

    export = sub.add_parser("export", help="导出流程为 JSON/YAML")
    export.add_argument("--flow", default="default")
    export.add_argument("--out", default="flows/export.json")
    export.set_defaults(func=_cmd_export)

    importer = sub.add_parser("import", help="从文件导入流程")
    importer.add_argument("file")
    importer.add_argument("--id")
    importer.add_argument("--name")
    importer.add_argument("--force", action="store_true")
    importer.set_defaults(func=_cmd_import)

    permission = sub.add_parser("permission", help="检查 macOS 权限")
    permission.add_argument("--json", action="store_true")
    permission.set_defaults(func=_cmd_permission)

    runs = sub.add_parser("runs", help="列出运行历史")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--status")
    runs.add_argument("--json", action="store_true")
    runs.set_defaults(func=_cmd_runs)

    trace = sub.add_parser("trace", help="打印某次运行的执行轨迹")
    trace.add_argument("run_id")
    trace.set_defaults(func=_cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
