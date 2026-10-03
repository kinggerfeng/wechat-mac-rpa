"""Every frontend app must keep its build output out of the index.

``apps/desktop/.gitignore`` carried ``node_modules`` and ``dist`` from the day
it was a lone Vite app. When the operations console was split out into
``apps/admin_console`` the rules did not follow: ignore rules are scoped to the
directory that declares them, and nothing in the root ``.gitignore`` mentioned
either name. The result was 12 133 untracked files — the whole dependency tree
plus a build output — sitting one ``git add apps/admin_console`` away from the
index, and ``git status`` reporting 12 157 entries for a 24-file change.

This project's number-one bug shape is a silent one: nothing errors, the
directory is simply absent from a fresh clone. So the invariant gets a test
that asks git itself rather than re-implementing the matching rules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories that are never application roots even if a ``package.json``
#: turns up inside them (a fixture, or a vendored sample).
_NOT_AN_APP = {"node_modules", "dist", "src-tauri"}


def _app_roots() -> list[Path]:
    """Every ``package.json`` in the repository except the ones we ship.

    Discovered rather than listed, so adding a third frontend app is covered
    without editing this file — which is the whole point, since forgetting to
    edit a list is how the previous app got missed.
    """
    roots: set[Path] = set()
    for manifest in REPO_ROOT.rglob("package.json"):
        parts = set(manifest.relative_to(REPO_ROOT).parts)
        if parts & _NOT_AN_APP:
            continue
        roots.add(manifest.parent)
    return sorted(roots)


def _ignored(path: Path) -> bool:
    """Ask git whether the path is ignored, the same way ``git add`` would."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(path.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def test_the_repository_has_at_least_two_frontend_apps():
    """If discovery breaks and returns nothing, every test below passes
    vacuously. Assert the shape we expect: a Tauri shell and a browser console.
    """
    names = {root.name for root in _app_roots()}
    assert {"desktop", "admin_console"} <= names, f"expected both frontends, found {sorted(names)}"


def test_no_frontend_app_carries_an_unguarded_dependency_tree():
    unignored = [root for root in _app_roots() if not _ignored(root / "node_modules")]
    assert not unignored, (
        "node_modules/ is not ignored for: "
        + ", ".join(str(root.relative_to(REPO_ROOT)) for root in unignored)
    )


def test_no_frontend_app_carries_an_unguarded_build_output():
    unignored = [root for root in _app_roots() if not _ignored(root / "dist")]
    assert not unignored, (
        "dist/ is not ignored for: "
        + ", ".join(str(root.relative_to(REPO_ROOT)) for root in unignored)
    )
