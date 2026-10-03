"""An audit finding that points nowhere is worse than no finding.

``CODE_AUDIT_ISSUES`` is a hand-maintained list carried over from ``admin.py``.
Each entry carries a ``file``, a human-readable ``lines`` range, and a
``github_url`` with a ``#L<line>`` anchor. All three still named ``src/...`` at
line numbers from before the tree was split into ``rpa/``, ``apps/engine/`` and
``services/company_api/``: the import rewrite moved code around, so ``354``
became ``361``, ``772`` became ``1198``, and ``962`` became ``1384``.

Nothing fails when that happens. The page renders, the severity badges look
authoritative, and every link is a 404 pointing at a directory that no longer
exists — which is the worst combination available, because the finding looks
like evidence.

So the location fields are checked against the working tree. The prose is not:
whether a P0 is still *true* is a judgment a human has to make, and these tests
only assert that a claim made is at least locatable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.company_api.cases_api import CODE_AUDIT_ISSUES

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``48-142, 893-904`` / ``361`` / ``602-611, 215`` — the shapes the list uses.
_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")
_ANCHOR = re.compile(r"#L(\d+)$")


def _spans(spec: str) -> list[tuple[int, int]]:
    spans = []
    for part in (piece.strip() for piece in spec.split(",")):
        match = _RANGE.match(part)
        assert match, f"unparseable line spec {part!r} in {spec!r}"
        start = int(match.group(1))
        spans.append((start, int(match.group(2) or start)))
    return spans


def _findings() -> list[dict]:
    assert CODE_AUDIT_ISSUES, "the audit list went empty; that is a deletion, not a refactor"
    return CODE_AUDIT_ISSUES


def test_the_audit_list_is_not_empty():
    """A vacuous parametrization over an empty list is the failure mode this
    whole file exists to prevent, so it is asserted rather than assumed."""
    assert len(_findings()) == 7


@pytest.mark.parametrize("issue", _findings(), ids=lambda i: i["key"])
def test_the_cited_file_exists(issue: dict):
    target = REPO_ROOT / issue["file"]
    assert target.is_file(), (
        f"{issue['key']} cites {issue['file']}, which is not a file. A finding that "
        f"points at a path that no longer exists reads as settled fact and is not."
    )


@pytest.mark.parametrize("issue", _findings(), ids=lambda i: i["key"])
def test_the_cited_lines_are_inside_the_cited_file(issue: dict):
    total = len((REPO_ROOT / issue["file"]).read_text(encoding="utf-8").splitlines())
    for start, end in _spans(issue["lines"]):
        assert 1 <= start <= end <= total, (
            f"{issue['key']} cites lines {issue['lines']} of {issue['file']}, "
            f"which has {total} lines"
        )


@pytest.mark.parametrize("issue", _findings(), ids=lambda i: i["key"])
def test_the_permalink_matches_the_cited_file(issue: dict):
    """The URL and the ``file`` field are the same claim written twice, so they
    are checked against each other — a rename that updated one and not the
    other is the exact shape of the bug."""
    url = issue["github_url"]
    anchor = _ANCHOR.search(url)
    assert anchor, f"{issue['key']} has a github_url with no #L anchor: {url}"
    assert f"/blob/main/{issue['file']}#" in url, (
        f"{issue['key']} points at {url} but cites file {issue['file']}"
    )


@pytest.mark.parametrize("issue", _findings(), ids=lambda i: i["key"])
def test_the_permalink_anchor_is_one_of_the_lines_it_cites(issue: dict):
    """The anchor is what a reader lands on. If it is outside the range the
    finding describes, the link opens on unrelated code and reads as if it
    were not."""
    line = int(_ANCHOR.search(issue["github_url"]).group(1))
    spans = _spans(issue["lines"])
    assert any(start <= line <= end for start, end in spans), (
        f"{issue['key']} links to L{line} but describes {issue['lines']}"
    )
