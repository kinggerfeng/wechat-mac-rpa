"""No executable file may still point at a retired tree.

Three roots have been renamed in this reorganisation, and each one fails only
where it is used rather than where it is written:

* ``scripts/`` -> ``tools/{bench,data,persona,wiki,ops,server}``
* ``src/`` -> ``rpa/``
* ``services/company_api/`` -> ``services/company_api/``

A stale reference is not a compile error. ``import scripts.sync_knowledge`` and
``subprocess.run(["python3", "scripts/doc_lint.py"])`` both work right up to the
moment the path is exercised, which for a nightly job can be days.

Markdown is excluded on purpose. The docs under ``docs/`` are design records
that also cite scripts which never existed (``scripts/view_ocr_history.py``,
``scripts/build_index.py``); rewriting those would invent history. They are
cleaned up as prose, not asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Matched against the first path segment only. ``data`` belongs here and not
#: in a set matched against every segment: ``tools/data/`` is a source directory
#: that happens to share a name with the runtime ``data/`` tree, and the naive
#: rule silently skipped 13 real files.
SKIP_TOP = {".git", ".venv", "node_modules", "data", "third_party"}

#: Matched against every segment — `node_modules` is nested under `app/`, so a
#: first-segment rule would walk 7000 vendored files.
SKIP_ANY = {"__pycache__", "node_modules", "target", "dist",
            ".pytest_cache", ".mypy_cache"}

#: The Vue app's owns the same word ``src/`` for a different tree entirely.
#: Compared as whole segments, because the prefix ``"apps/desktop/src"`` also matches
#: ``apps/desktop/src-tauri/``, which is the Rust crate that does need rewriting.
SKIP_TREES = (("apps", "desktop", "src"), ("apps", "desktop", "dist"), ("apps", "desktop", "src-tauri", "target"))

#: Only files that can break at run time. ``.rs`` is here because the Tauri
#: shell is what launches the Python API — ``services.company_api.app:app`` is a string
#: the Rust binary passes to uvicorn, and nothing else would notice it rot.
#: ``tests/`` is skipped so this test does not assert on its own source.
CODE_SUFFIXES = {".py", ".sh", ".command", ".yml", ".yaml", ".toml", ".ts", ".vue", ".rs"}

#: `scripts/foo.py` in prose and in subprocess arguments, `scripts.foo` in an
#: import. Whitespace is tolerated because tokens are rejoined with a
#: separator, which turns `from scripts.sync_knowledge` into
#: `scripts . sync_knowledge`.
RETIRED_SCRIPTS = re.compile(r"\bscripts\s*[./]\s*[A-Za-z0-9_./]*")

#: `src.` as a module path, the retired `python.backend` package, and the bare
#: `"src"` root logger.
#:
#: Every `\s*` below is load-bearing. Python is tokenised and the tokens are
#: rejoined with a separator, so `getLogger("src")` arrives as
#: `getLogger ( "src" )` and `python.backend` as `python . backend`. A pattern
#: written without the space matches nothing, which is exactly how this file
#: first shipped a guard that guarded nothing.
#:
#: The `python` root is matched as `python.backend` and not as `python.<any>`:
#: `python` is also a local variable holding the interpreter path in the Tauri
#: shell (`python.is_file`) and appears in command strings (`python .venv/...`).
#: `backend` was the only package ever under it.
RETIRED_PYTHON = re.compile(
    r"(?<![\w.])src\.[A-Za-z_]+"
    r"|python\s*\.\s*backend"
    r"|python\s*/\s*backend"
    r"|getLogger\s*\(\s*[\"']src[\"']\s*\)"
)


def _code_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in SKIP_TOP or relative.parts[0] == "tests":
            continue
        if any(part in SKIP_ANY for part in relative.parts):
            continue
        if any(relative.parts[:len(tree)] == tree for tree in SKIP_TREES):
            continue
        if relative.parts == ("apps", "desktop", "tsconfig.json"):
            continue
        files.append(relative)
    return sorted(files)


def _docstring_lines(source: str) -> set[int]:
    """Lines occupied by a docstring, by AST position rather than by guessing."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                  ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _executable_text(path: Path) -> str:
    """Source with comments and docstrings removed; other literals kept.

    A retired path in a docstring is documentation — including the note
    recording that a launcher was deleted — and rewriting it would falsify the
    record. But in Python a path is nearly always an ordinary string literal
    (``["python3", "scripts/doc_lint.py"]``), so dropping every string would
    blind the test to exactly the references it exists to catch.

    Shell, Vue and YAML have no AST to consult, so they are scanned whole.
    """
    if path.suffix != ".py":
        return path.read_text(encoding="utf-8")

    import io
    import tokenize

    source = path.read_text(encoding="utf-8")
    docstrings = _docstring_lines(source)

    kept: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING and token.start[0] in docstrings:
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    return " ".join(kept)


@pytest.mark.parametrize("relative", _code_files(), ids=lambda p: p.as_posix())
def test_no_executable_file_references_scripts(relative: Path):
    path = REPO_ROOT / relative
    try:
        text = _executable_text(path)
    except UnicodeDecodeError:
        pytest.skip("not text")

    stale = sorted(set(RETIRED_SCRIPTS.findall(text)))
    assert not stale, (
        f"{relative.as_posix()} still references {stale} — the tree moved to "
        f"tools/<category>/, and this fails only at run time"
    )


@pytest.mark.parametrize("relative", _code_files(), ids=lambda p: p.as_posix())
def test_no_executable_file_references_the_old_python_root(relative: Path):
    """``src`` is also a very common local variable name, so this is narrow.

    A bare ``rpa.parent`` in code is a ``Path`` variable and stays; only
    ``rpa.<module>`` — which in code can only appear inside a string, because a
    local variable's value is never interpolated into a literal — is stale.
    """
    path = REPO_ROOT / relative
    try:
        text = _executable_text(path)
    except UnicodeDecodeError:
        pytest.skip("not text")

    stale = sorted(set(RETIRED_PYTHON.findall(text)))
    assert not stale, (
        f"{relative.as_posix()} still references {stale} — the package is now "
        f"rpa/, and the desktop API is services.company_api"
    )


def test_the_retired_trees_are_actually_gone():
    assert not (REPO_ROOT / "scripts").exists(), "scripts/ came back"
    assert not (REPO_ROOT / "src").exists(), "src/ came back"
    assert not (REPO_ROOT / "python").exists(), "python/ came back"
    assert (REPO_ROOT / "rpa").is_dir()
    assert (REPO_ROOT / "tools").is_dir()


def test_the_vue_app_still_typechecks_its_own_sources():
    """`apps/desktop/src/` is the frontend's, and must survive the Python rename.

    Pointing ``tsconfig.json`` at ``rpa/**`` does not fail. Vite builds from
    its own config, and ``vue-tsc`` reads an ``include`` that matches no files
    and reports a clean run — a type check of nothing, which is the same
    failure dressed as a pass.
    """
    config = (REPO_ROOT / "apps" / "desktop" / "tsconfig.json").read_text(encoding="utf-8")

    assert '"src/**/*.ts"' in config, "tsconfig no longer includes apps/desktop/src"
    assert '"src/**/*.vue"' in config, "tsconfig no longer includes apps/desktop/src components"
    assert "rpa/" not in config, "the Python package leaked into the frontend tsconfig"
    assert (REPO_ROOT / "apps" / "desktop" / "src").is_dir(), "the frontend source directory is gone"


def test_the_three_trees_import_from_one_root():
    """Engine, desktop API and the legacy bot must resolve from one sys.path root.

    They are three top-level packages now — ``apps.engine``,
    ``services.company_api`` and ``rpa`` — which is what makes each of them
    separately packageable. The cost of that is that they can drift: if one
    ever reaches the others by inserting its own directory onto ``sys.path``
    instead of relying on the repository root, the scheduler lock and the flow
    engine stop seeing the same ``rpa.db``, and every surface still looks fine.

    So the invariant is not "one package" any more. It is: importable together,
    from the repository root, with nobody cheating on ``sys.path``.
    """
    import apps.engine
    import rpa
    import services.company_api

    root = str(REPO_ROOT)
    for module in (apps.engine, services.company_api, rpa):
        location = getattr(module, "__file__", None) or str(getattr(module, "__path__", [""])[0])
        assert str(Path(location).resolve()).startswith(root), (
            f"{module.__name__} resolved outside the repository root: {location}"
        )


def test_no_tree_inserts_its_own_directory_into_sys_path():
    """Importing a package must not change what its siblings can see.

    The cheat this catches is a package that reaches another one by inserting
    its *own* directory onto ``sys.path``: it then imports fine on its own and
    is invisible to everything else, so the failure surfaces later as one
    process's engine not finding a file, looking like a packaging bug.

    Checked by importing for real rather than by reading the lines, because
    every one of these packages legitimately contains a guarded
    ``sys.path.insert(0, PROJECT_ROOT)`` for the case where it is run
    standalone — text matching cannot tell that apart from a sibling-directory
    insert, and guessing would make the test either useless or noisy.

    Run in a subprocess so the parent's already-warm ``sys.path`` cannot mask
    a mutation.
    """
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "before = list(sys.path)\n"
        "import rpa, apps.engine, services.company_api\n"
        "added = [p for p in sys.path if p not in before]\n"
        "print(repr(added))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"import probe failed:\n{result.stderr[-2000:]}"
    assert result.stdout.strip() == "[]", (
        f"importing the packages added {result.stdout.strip()} to sys.path. "
        f"Everything resolves from the repository root; a package that patches "
        f"sys.path is importable alone and invisible to its siblings."
    )
