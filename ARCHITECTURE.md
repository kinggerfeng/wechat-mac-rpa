# Architecture

The orientation map: what runs, what talks to what, and which boundaries are
load-bearing. Everything here was read out of the working tree — the node count,
the route count and the table count are measured, not remembered.

For "which file do I open" see
[`docs/02-architecture/MODULE_INDEX.md`](docs/02-architecture/MODULE_INDEX.md);
for copy-pasteable signatures see
[`docs/02-architecture/API_SURFACE.md`](docs/02-architecture/API_SURFACE.md);
for per-module contracts see `docs/02-architecture/specs/`.

## What this system is

A commercial-grade RPA product for the macOS WeChat client, built on one
differentiator: **two locating paths that are peers, not fallback.**

- **Path A, element-driven.** Resolve against a local element library. Fast,
  deterministic, free. The Windows are named regions with anchors.
- **Path B, multimodal.** Screenshot → vision model → action. Needed when an
  element cannot be captured at all: self-drawn UI, canvas, remote desktops.

Both return the same value from one entry point,
`apps/engine/strategy.py:resolve()` → `Located(x, y, source, confidence, …)`,
so no downstream node knows or cares which path found the target.

**The path is a design-time choice, not a runtime fallback.** `path` is a
first-class field on `Node`, alongside `target`; `graph.default_path` is the
flow-level default. Four modes: `auto` (element first, then model), `element`,
`element_strict` (element failure is fatal — it will *never* call a model),
`vision` (ignore the element library). `element_strict` is what makes the cost
predictable, and the canvas surfaces that per node.

WeChat is the reason both paths exist: the message area has no structure to
capture, while the search box, the input box and the back key are perfect
named regions.

## Layout

```
apps/
  engine/         RPA orchestration engine (Python) — registry, executor, expr,
                  strategy, 74 node types, and layout/ beneath it
  desktop/        Tauri + Vue 3 shell for the end user — the canvas and the nine
                  pages that drive the machine
  admin_console/  operations console for the operator — thirteen pages over
                  data/cases.db; a browser app, never shipped in the bundle
  shared/         the two frontends' common code: the single API client, the
                  hand-written contracts, the theme tokens, and the port
                  declaration they must agree on
services/
  company_api/    FastAPI, launched by Tauri via uvicorn; the ops console talks
                  to it too, with the internal routers mounted
rpa/              the legacy L1–L5 bot and the platform primitives it uses
                  (bot/ db/ memory/ reply/ action/ capture/ ocr/ perception/ …)
tools/            one-off scripts, grouped bench / data / persona / wiki / ops
tests/            the whole test suite; tests/e2e needs real hardware
data/             runtime state, gitignored
```

| Tree | Lines | Files |
|---|---:|---:|
| `rpa/` | 18,551 | 70 |
| `tests/` | 18,339 | 78 |
| `tools/` | 13,247 | 54 |
| `apps/engine/` | 12,989 | 31 |
| `apps/desktop/src/` | 8,144 | 24 |
| `apps/admin_console/src/` | 6,737 | 17 |
| `services/company_api/` | 2,450 | 5 |
| `apps/shared/` | 1,146 | 3 |

**Why three apps and not one.** `apps/engine` is the product and is useful
without a UI — a caller can drive a flow from a script, which is what
`apps/engine/cli.py` is for. `apps/desktop` is one *consumer* of it. Folding
the engine into the desktop app would make "headless automation" impossible,
and would put 13k lines of orchestration behind a Tauri bundle.

**Why the console is a separate app, not a second nav group.** An end user of
the RPA product never reviews ticks or judges badcases — that is the operator's
job, over a different database. When the console pages lived in the desktop
shell, a shipped build produced eight links that all 404, because the backend
already classified `cases_api` as internal while the frontend had no such
switch. The backend was right and the frontend was wrong.

**Why the API is not under `apps/`.** It is not an app; it is a process the
desktop shell spawns and then talks to over loopback. Putting it beside the
apps says that.

**`apps/engine` and `rpa/` coexist and neither replaces the other.** The
canvas edits a graph; `rpa/bot/wechat_bot.py` is the loop it grew out of, and
the seeded main flow is a migration of that loop rather than a rewrite of it.

All three Python trees resolve from the repository root — none of them patches
`sys.path` to reach another, which
`tests/test_no_stale_path_refs.py::test_no_tree_inserts_its_own_directory_into_sys_path`
enforces by importing them for real.

Directory names holding Python modules use identifiers (`company_api`), and
the console is `admin_console` rather than `admin-console` because a hyphen
makes the package unimportable. Its own pages are all under `src/`, so the
hyphen would only have cost the one import.

`tools/` and `rpa/tools/` are unrelated things that share a word: the former is
operator scripts, the latter is the bot's tool registry.

## Process model

```
RPAStudio.app (Tauri)
  └─ spawns uvicorn → services.company_api.app:app on 127.0.0.1:8767
        ├─ rpa_api      38 routes  SHIPPED
        ├─ record_api    9 routes  SHIPPED
        └─ cases_api    23 routes  INTERNAL   (RPA_DESKTOP_INTERNAL, default on)
  └─ serves apps/desktop/dist, which talks to the loopback port
```

The port is declared once, in `apps/shared/api-port.txt`, because the Rust
shell (`include_str!`), the desktop client and the console client all have to
agree — a build-time mismatch is invisible at runtime and is exactly how two
engines end up sharing one `rpa.db`. `tests/test_api_port.py` asserts all three
read that one place and that neither frontend carries a literal.

66 paths / 76 operations total. The shipped/internal split is a real product
boundary, not a naming habit: a packaged build must not expose the operator
surface to an end user.

## Who owns which database

| File | Owner | Notes |
|---|---|---|
| `data/rpa.db` | `apps/engine/` | 8 tables: flows, flow_runs, spans, elements, schedules, recordings, llm_providers |
| `data/cases.db` | `services/company_api/cases_api.py` | the only module that opens it |
| chat history | `rpa/db/` | SQLite is the single source of truth |
| `models/` weights | shipped with the product | index cache lives in `data/memory/cache/` |

`cases.db` is optional: a desktop install that never ran the bot has no such
file. Every route in `cases_api.py` therefore survives its absence — reads
answer 200 with the empty shape plus `degraded: true`, writes answer 503.
Frontends must render that flag, or "unreadable" and "empty" look identical.

## The node catalogue

74 registered types across 14 categories: 控制流 15, 动作 15, 文件 8, 表格 6,
通用 5, 双路径 4, 感知 4, 会话 4, 多模态 3, 回复 3, 系统 2, 记忆 2, 记录 2,
代码 1.

`NodeSpec.to_dict()` exposes `path_aware` and `path_choices`, which is how the
canvas knows to render a path selector on the node itself and a cost warning
when an `auto` node can reach a paid model.

Adding a type means: implement the node, register it via
`NodeRegistry.register(NodeSpec(...))` — which also writes the spec back onto
the handler class, because `BaseNode.param()` reads `cls.spec` at run time and
misses there — and add its spec under `docs/02-architecture/specs/`.

## Egress

Every outbound call goes through `rpa/net/endpoints.py`: 7 endpoints
(`dashscope`, `kimi`, `deepseek`, `so`, `wttr`, `gtimg`, `tuyacn`), each with
`base_url()` and `is_configured()`, split into *model* endpoints (redirectable
by env var) and *third-party* endpoints (need a central kill switch).
`require_enabled()` is enforced inside `base_url()` so a half-migrated call
site cannot bypass it.

The rule: a model endpoint may be redirected, a third-party endpoint must be
disabled centrally. `rpa/net/endpoints.py::inventory()` answers "where does our
data go" by reading one file instead of seven.

## The invariants that bite

**The scheduler runs in exactly one process.** Several processes share
`data/rpa.db`; only the one holding the file lock starts the scheduler thread.
Otherwise one cron tick fires N times and sends N duplicate messages.

**A node contains no business logic.** A node wraps an existing subsystem and
shapes its return value. The decision stays in the original module, along with
its tests.

**`element_strict` never calls a model.** This is what makes the cost of a flow
predictable, and it is a property of the design-time path choice, not a runtime
guard.

**Code execution has exactly one entry point.** `run_shell` defaults to
`shlex.split` and never invokes a shell. There is no container sandbox: the
positioning here is a local RPA studio, not a multi-tenant SaaS.

## The failure mode to design against

**A failure dressed up as data.** An endpoint that swallows an exception and
returns an empty list, a node that times out and records success with empty
output, a chart that draws zeros because the query failed — all exit 0 and all
lie. When a read can fail, the failure has to be visible in the response, not
only in the log.

The sharpest instance of this was a worker thread: an exception inside
`_call_with_timeout` does not cross the thread boundary, so the worker looked
like it finished on time, the node was recorded as *successful with empty
output*, and the whole flow reported `ok` while perception never ran.

Guards, each written after being bitten:

- `tests/test_tool_layout.py` — a tool that climbs one level too few reads
  `tools/data/` instead of `data/` and still exits 0.
- `tests/test_no_stale_path_refs.py` — a retired path fails where it is used,
  not where it is written.
- `tests/test_design_tokens.py` — a `var(--x)` that was never declared is
  silently dropped, which once made a whole navigation column invisible while
  the build, the type check and the test suite all stayed green.
- `degraded` on every cases response.

## Permissions

macOS TCC is granted to a **bundle**, not to the interpreter: the grant is for
`/Applications/RPAStudio.app`. A process started from a terminal inherits the
responsible process of the terminal's parent, so it shows the wrong app name in
the prompt *and* reports denials that are not real. Test permissions by
launching from the bundle with `open -a`, not from bash.

`permissions.check_all(deep=False)` is a shallow check and safe to poll;
`deep=True` reads the TCC database and can raise a consent dialog, so only the
permission page may call it.

## Environment

Behaviour switches (all read with a safe default, so an unset variable means
"the documented default", not "broken"):

`LLM_MODEL` `LLM_API_KEY` `DASHSCOPE_API_KEY` `DEEPSEEK_API_KEY` `KIMI_BIN`
`VISION_MODEL` `OPENAI_API_KEY` `OPENCLAW_API_KEY` `OPENCLAW_BASE_URL`
`OPENCLAW_MODEL` · feature gates `ENABLE_REACT_TOOLS` `ENABLE_SELF_REFINE`
`ENABLE_JUDGE_WORKER` `ENABLE_FACT_CHECK` `ENABLE_PERSONA_FEW_SHOTS` · bot
`WECHAT_SILENT_MODE` `SILENT_WHITELIST` `WECHAT_NO_REPLY_CHATS`
`MAX_RUNTIME_MESSAGES` `WEFLOW_MODE` `WEFLOW_ACCESS_TOKEN` · platform
`RPA_DESKTOP_INTERNAL` `RPA_WEBHOOK_TOKEN` `RPA_STUDIO_PORT`

## Testing

`tests/` is the CI layer — 1653 passing, 51 skipped. `tests/e2e/` needs real
hardware and is only collected, not run. There is no `unit/` versus
`integration/` split: the labels were not trustworthy, and an untrustworthy
label is worse than none.

Install with uv, not pip — the interpreter is uv-managed:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q
```

Both frontends are checked the same way, and both must pass:

```bash
for app in desktop admin_console; do
  (cd "apps/$app" && npx vue-tsc --noEmit -p tsconfig.json && npm run build)
done
```

Changing anything under `apps/shared/` requires running that loop over *both*
applications.
