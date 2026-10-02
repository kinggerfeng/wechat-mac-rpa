# Architecture

The orientation map. For the full design rationale see
[`docs/02-architecture/ARCHITECTURE.md`](docs/02-architecture/ARCHITECTURE.md);
for "which file do I open" see
[`docs/02-architecture/MODULE_INDEX.md`](docs/02-architecture/MODULE_INDEX.md);
for copy-pasteable signatures see
[`docs/02-architecture/API_SURFACE.md`](docs/02-architecture/API_SURFACE.md).

## Layout

```
rpa/            the single Python package — engine, bot, memory, desktop API
  flow/         RPA orchestration: registry, executor, expression engine
  backend/      desktop FastAPI, launched by Tauri via uvicorn
  bot/ db/ memory/ reply/ action/ capture/ ocr/ …   the L1–L5 bot layers
app/            Tauri + Vue 3 desktop shell
tools/          one-off scripts, grouped bench / data / persona / wiki / ops / server
tests/          the whole test suite; tests/e2e needs real hardware
data/           runtime state, gitignored
```

`rpa/` is a plain top-level package, not a `src/` layout. `tools/` and
`rpa/tools/` are unrelated things that share a word: the former is operator
scripts, the latter is the bot's tool registry.

## The two invariants that bite

**The scheduler runs in exactly one process.** Several processes share
`data/rpa.db`; only the one holding the file lock starts the scheduler thread.
Otherwise one cron tick fires N times and sends N duplicate messages.

**A node contains no business logic.** A node wraps an existing subsystem and
shapes its return value. The decision stays in the original module, along with
its tests.

## Who owns which database

| File | Owner | Notes |
|---|---|---|
| `data/rpa.db` | `rpa/flow/` | the orchestration engine |
| `data/cases.db` | `rpa/backend/cases_api.py` | the only module that opens it |
| chat history | `rpa/db/` | SQLite is the single source of truth |

`cases.db` is optional: a desktop install that never ran the bot has no such
file. Every route in `cases_api.py` therefore survives its absence — reads
answer 200 with the empty shape plus `degraded: true`, writes answer 503.
Frontends must render that flag, or "unreadable" and "empty" look identical.

## The failure mode to design against

**A failure dressed up as data.** An endpoint that swallows an exception and
returns an empty list, a node that times out and records success with empty
output, a chart that draws zeros because the query failed — all exit 0 and all
lie. When a read can fail, the failure has to be visible in the response, not
only in the log.

Three guards exist for this, and each was written after being bitten:

- `tests/test_tool_layout.py` — a tool that climbs one level too few reads
  `tools/data/` instead of `data/` and still exits 0.
- `tests/test_no_stale_path_refs.py` — a retired path fails where it is used,
  not where it is written.
- `degraded` on every cases response.

## Testing

`tests/` is the CI layer. `tests/e2e/` needs real hardware and is only
collected, not run. There is no `unit/` versus `integration/` split: the labels
were not trustworthy, and an untrustworthy label is worse than none.

Install with uv, not pip — the interpreter is uv-managed:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q
```

## Permissions

macOS TCC is granted to a **bundle**, not to the interpreter: the grant is for
`/Applications/RPAStudio.app`. A process started from a terminal inherits the
responsible process of the terminal's parent, so it shows the wrong app name in
the prompt *and* reports denials that are not real. Test permissions by
launching from the bundle with `open -a`, not from bash.
