"""A deleted document that is still linked is a dead link with a delay.

Twenty-three documents were removed in one pass — design proposals for a merge
that never happened, a data model that was superseded by the three tables that
actually shipped, a byte-identical duplicate, and status trackers that were
stale the moment they were written. The removal was mechanical and the links
were not: eight of them survived in three files, and because a markdown link
that resolves to nothing still renders as blue underlined text, the failure is
invisible until someone clicks it.

Bare mentions were checked by hand in the same pass — three of them turned out
to be worse than the links, because ``AGENTS.md`` is gitignored as private local
material, so ``WORKFLOW.md`` instructed a reader with a fresh checkout to record
lessons in a file they do not have. They are *not* checked here: matching a
document name in prose cannot be told apart from a filename in a code sample or
a tick dump, and the false positives (``data/persona.md``, ``prompt.md``, a
``tick_*.md`` path) made an allowlist worse than the gap. A link is a promise
the renderer checks; prose is not.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: `docs/archive/` is explicitly "retired implementations and historical
#: reviews" by its own README, and a historical record is allowed to point at
#: things that no longer exist. Everything else is a live claim.
ARCHIVE = ("docs", "archive")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "target", "dist"}

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")


def _documents() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.md")
        if not (SKIP_DIRS & set(p.relative_to(REPO_ROOT).parts))
    ]


def _live() -> list[Path]:
    return [p for p in _documents() if ARCHIVE not in p.relative_to(REPO_ROOT).parts]


def test_the_scan_would_actually_find_the_documents():
    """If the glob or the filters stopped matching, the test below would pass
    on an empty set and look like a clean repository."""
    assert len(_live()) > 30, f"only found {len(_live())} live documents; the scan is wrong"


def test_no_live_document_links_to_a_file_that_does_not_exist():
    broken: dict[str, set[str]] = {}
    for path in _live():
        for raw in _LINK.findall(path.read_text(encoding="utf-8", errors="ignore")):
            target = raw.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).resolve().exists():
                broken.setdefault(str(path.relative_to(REPO_ROOT)), set()).add(raw)

    assert not broken, "markdown links that resolve to nothing: " + "; ".join(
        f"{src} -> {sorted(tgts)}" for src, tgts in sorted(broken.items())
    )
