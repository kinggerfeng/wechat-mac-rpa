"""A CSS variable that is used but never defined renders as nothing at all.

``tokens.css`` moved to ``apps/shared/`` when the operations console was split
out, and the desktop kept its real token names. The console was written fresh
and invented plausible-looking ones: ``--green-deep``, ``--green-line``,
``--green-mid``, ``--green-muted``, ``--ink-muted``, ``--font-display``. None
of them exist.

An undefined custom property is not an error. ``background: var(--green-deep)``
becomes invalid at computed-value time, so the declaration is dropped and the
rail inherited the page canvas behind it — the whole left column came out
light gray. ``color: var(--paper)`` *was* defined, so the brand mark and the
``OPS CONSOLE`` label rendered near-white on near-white and disappeared
entirely.

Nothing reported it: the Vite build succeeds, ``vue-tsc`` does not type-check
custom property names, the console log is clean, and every number on every
page still came from the API. A whole navigation column was invisible and the
test suite stayed green.

So the reference is checked against the declaration, in both applications,
because the failure is a name that does not exist rather than a value that
looks wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APPS_ROOT = REPO_ROOT / "apps"
TOKENS = APPS_ROOT / "shared" / "styles" / "tokens.css"

#: Both applications, discovered rather than listed — a third frontend has to
#: be covered without editing this file, which is how the console was missed.
APP_ROOTS = sorted(p for p in APPS_ROOT.iterdir() if (p / "src").is_dir())

_DECLARED = re.compile(r"^\s*(--[A-Za-z0-9_-]+)\s*:", re.M)
_REFERENCED = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
_STYLE_SUFFIXES = {".vue", ".css", ".scss"}


def _declared() -> set[str]:
    assert TOKENS.is_file(), f"the shared token sheet is missing: {TOKENS}"
    names = set(_DECLARED.findall(TOKENS.read_text(encoding="utf-8")))
    assert names, "tokens.css declares no custom properties; the regex is wrong, not the file"
    return names


def _style_files() -> list[Path]:
    files: list[Path] = []
    for app in APP_ROOTS:
        files += [
            p for p in (app / "src").rglob("*")
            if p.is_file() and p.suffix in _STYLE_SUFFIXES
        ]
    return sorted(files)


def _referenced(path: Path) -> set[str]:
    return set(_REFERENCED.findall(path.read_text(encoding="utf-8", errors="ignore")))


def test_both_applications_load_the_shared_token_sheet():
    """Without the import every variable in both apps is undefined at once, and
    the result is the same class of blank page by a much larger route. The
    import is the thing that makes ``tokens.css`` load at all, so it is
    asserted rather than assumed."""
    assert len(APP_ROOTS) == 2, f"expected the desktop shell and the console, found {APP_ROOTS}"
    for app in APP_ROOTS:
        entry = app / "src" / "main.ts"
        text = entry.read_text(encoding="utf-8")
        assert "tokens.css" in text, f"{app.name} never imports the shared token sheet"


def test_no_style_rule_references_a_token_that_does_not_exist():
    declared = _declared()
    broken: dict[str, set[str]] = {}
    for path in _style_files():
        for name in _referenced(path) - declared:
            # Element Plus defines its own --el-* scale on :root; those are
            # resolved by the library, not by us.
            if name.startswith("--el-"):
                continue
            broken.setdefault(name, set()).add(path.relative_to(REPO_ROOT).as_posix())

    assert not broken, (
        "custom properties used but never declared in apps/shared/styles/tokens.css "
        "(an undefined var() is silently dropped, it does not fail the build): "
        + "; ".join(f"{name} used by {sorted(files)}" for name, files in sorted(broken.items()))
    )


def test_the_scan_would_notice_a_missing_token_sheet():
    """Guards the guard: if the scan finds no style files, or the reference
    regex stops matching, the test above passes for the wrong reason — which is
    the failure mode this file exists to oppose."""
    files = _style_files()
    assert len(files) > 20, f"style scan only found {len(files)} files; it is not looking at the apps"
    referenced = set().union(*(_referenced(p) for p in files))
    assert len(referenced) > 10, f"only found {len(referenced)} var() references; the regex is wrong"
    assert referenced - _declared() <= {n for n in referenced if n.startswith("--el-")}
