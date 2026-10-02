# tools

Operational scripts, grouped by domain. Nothing here is imported by the bot or
by the desktop app; these are things a person runs.

The grouping replaced a flat `scripts/` directory of 46 unrelated top-level
files plus `scripts/db/`. The six directories are the whole taxonomy — a script
that fits none of them does not belong in this tree.

| Directory | Holds |
|---|---|
| `bench/` | Benchmark collection, HTML reports, A/B experiments. `legacy/` holds retired one-off experiment scripts, kept for reference only. |
| `data/` | Importing chat history, database migrations, backup, deduplication. Absorbs the former `scripts/db/`. |
| `persona/` | Building, de-identifying and evaluating the few-shot persona sample set. |
| `wiki/` | Generating wiki pages, contradiction detection, fact cleaning, memory lint. |
| `ops/` | Environment setup, key retrieval, the privacy gate, the doc linter, the GitHub star chart. |
| `server/` | Service entry points. `admin.py` is the legacy developer backend and is being retired in favour of `rpa/backend/`. |

## Rules

**A tool locates the repository root with `Path(__file__).parents[2]`.** From
`tools/<category>/<name>.py` that is the repository root. This is checked by
`tests/test_tool_layout.py`: one level short and the tool reads
`tools/data/`, finds nothing, and exits 0 — a silent no-op rather than a crash.

**`tools/bench/legacy/` is the exception.** It moved sideways from
`scripts/experiments/legacy/`, so it was already three levels deep and kept its
level count.

**Scripts that call each other use a path built from the repository root**, not
a relative one, so they work from any working directory:

```python
cmd = [sys.executable, str(PROJECT_ROOT / "tools" / "bench" / "run_experiment.py")]
```

## Running them

```bash
.venv/bin/python tools/bench/run_experiment.py --exp <name> --all-labeled
.venv/bin/python tools/wiki/batch_generate_wiki.py --limit 20
.venv/bin/python tools/data/backup_chat_db.py --retention-days 30
```

Dependencies are installed with uv:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

Not `pip`. The interpreter is uv-managed (`.venv/pyvenv.cfg`).
