"""One definition of where the BGE weights and the vector index live.

The two paths were defined independently in two modules and they disagreed.
The index builder looked for the model under ``models/``; the searcher looked
under ``data/memory/models/``. Neither was wrong on its own, and nothing
raised — a user who followed ``models/README.md`` got a working build step and
a searcher that reported no model, with nothing to connect the two facts.

The failure that has no error message is the one worth a test: build an index
with one copy of the weights and search with another, and the embedding
spaces do not match. Retrieval then returns confident nonsense. No assertion
catches it; the numbers are simply wrong.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Environment variables that resolve a BGE path. Only `rpa/memory/paths.py`
#: may read them; a second reader is how the split reappears.
PATH_ENV_VARS = ("WECHAT_BGE_MODEL_PATH", "WECHAT_HISTORY_INDEX_PATH")

PATHS_MODULE = "rpa/memory/paths.py"


def _string_constants(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _module_level_names(path: Path) -> set[str]:
    """Names bound at module scope — a real assignment, not a mention in prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _real_consumers() -> list[str]:
    found = []
    for path in sorted((REPO_ROOT / "rpa").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(var in text for var in PATH_ENV_VARS):
            found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_paths_module_is_the_only_place_that_resolves_them():
    """Only ``rpa/memory/paths.py`` may read these environment variables.

    A second reader is how the split reappears: someone needs a model path in
    a new module, writes the two-line lookup by hand, and points it at
    whatever directory happened to work on their machine.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "rpa").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == PATHS_MODULE:
            continue
        for name in _string_constants(path):
            if name in PATH_ENV_VARS:
                offenders.append((relative, name))

    assert not offenders, (
        f"{offenders} resolve a BGE path directly. Import rpa.memory.paths "
        f"and call model_path() / index_path() instead — a second copy is how "
        f"the builder and the searcher ended up looking in different places."
    )


def test_the_tool_does_not_carry_its_own_default():
    """``tools/data/update_history_index.py`` builds the index the searcher reads.

    A private default here is the exact bug being fixed: the builder resolves
    one directory, the searcher another, and the two never meet. Checked
    against the module's actual bindings, not its prose — the comment
    explaining the old bug still names the constant it removed.
    """
    tool = REPO_ROOT / "tools" / "data" / "update_history_index.py"
    text = tool.read_text(encoding="utf-8")

    assert "rpa.memory.paths" in text, "the tool does not import the single source"
    assert "DEFAULT_MODEL" not in _module_level_names(tool), (
        "the tool defines its own model default again; the builder and the "
        "searcher must resolve the same directory"
    )


def test_builder_and_searcher_resolve_the_same_model(monkeypatch):
    monkeypatch.delenv("WECHAT_BGE_MODEL_PATH", raising=False)
    from rpa.memory import paths
    import rpa.memory.history_search as search

    assert search._model_path() == paths.model_path()


def test_builder_and_searcher_resolve_the_same_index(monkeypatch):
    monkeypatch.delenv("WECHAT_HISTORY_INDEX_PATH", raising=False)
    from rpa.memory import paths
    import rpa.memory.history_lookup as lookup
    import rpa.memory.history_search as search

    assert search._index_path() == paths.index_path()
    assert lookup._index_path() == paths.index_path()


def test_an_override_reaches_both_sides(monkeypatch, tmp_path):
    """The env var is the only supported way to relocate either path.

    Tested on both sides because the builder previously evaluated the
    environment at import time, so a value set after import was ignored.
    """
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("WECHAT_BGE_MODEL_PATH", str(elsewhere / "bge"))
    monkeypatch.setenv("WECHAT_HISTORY_INDEX_PATH", str(elsewhere / "index.pkl"))

    from rpa.memory import paths
    import rpa.memory.history_lookup as lookup
    import rpa.memory.history_search as search

    assert search._model_path() == elsewhere / "bge"
    assert search._index_path() == elsewhere / "index.pkl"
    assert lookup._index_path() == elsewhere / "index.pkl"
    assert paths.model_path() == elsewhere / "bge"


def test_the_weights_are_not_under_the_gitignored_data_tree():
    """`data/` is runtime state; a user wiping it must not lose the model.

    The split is by nature: weights ship with the product, the index is
    derived per machine. Keeping them under one directory would make the
    model disappear exactly when it is needed most.
    """
    from rpa.memory import paths

    assert "data" not in paths.DEFAULT_MODEL_PATH.relative_to(REPO_ROOT).parts
    assert paths.DEFAULT_MODEL_PATH == REPO_ROOT / "models" / "bge-small-zh-v1.5"
    assert "data" in paths.DEFAULT_INDEX_PATH.relative_to(REPO_ROOT).parts


def test_the_readme_documents_the_path_the_code_uses():
    readme = (REPO_ROOT / "models" / "README.md").read_text(encoding="utf-8")
    from rpa.memory import paths

    assert paths.DEFAULT_MODEL_PATH.name in readme, (
        "models/README.md points somewhere the code no longer looks"
    )


def test_who_resolves_these_paths():
    """Guard against the test silently covering nothing after a rename."""
    consumers = _real_consumers()
    assert "rpa/memory/paths.py" in consumers
    assert any("history_search" in c for c in consumers)
