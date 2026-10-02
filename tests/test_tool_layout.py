"""Every tool must locate the repository root the same way the tests do.

`scripts/<name>.py` -> `tools/<cat>/<name>.py` added a directory level to 61
files at once. Each one computed its project root as
``Path(__file__).parent.parent``, which after the move resolved to
``<repo>/tools`` instead of ``<repo>``. Nothing raises: the tool reads
``<repo>/tools/.env``, finds no ``data/`` directory, and exits 0 having done
nothing. That is this project's number-one bug shape, so it gets a test.

The expressions are read out of the source rather than by importing the
modules, because importing them runs argparse, mutates ``sys.path`` and in a
few cases reaches the network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPO_ROOT / "tools"
CATEGORIES = ("bench", "data", "persona", "wiki", "ops", "server")

#: ``tools/bench/legacy/`` came from ``scripts/experiments/legacy/`` and was
#: already three levels deep, so it kept its original level count and is
#: checked separately below.
LEGACY_ROOT = TOOLS_ROOT / "bench" / "legacy"

#: A whole `Path(__file__)...` chain, in either spelling of the level count:
#: `parents[2]` or repeated `.parent`. The chain is the whole point — truncating
#: it at the `Path(__file__)` call would hide the count under test, and
#: matching `.parent` without a word boundary would swallow the `p` of
#: `.parents[2]` and report one level short of the truth.
CHAIN = re.compile(
    r"Path\(__file__\)(?:\.resolve\(\))?(?:\.parents\b|\.parents\[\d+\]|\.parent\b)*"
)


def _tools() -> list[Path]:
    files: list[Path] = []
    for category in CATEGORIES:
        files.extend(sorted((TOOLS_ROOT / category).glob("*.py")))
    return files


def _chains(path: Path) -> list[str]:
    """Fixed-depth chains only; a bare ``.parents`` walk is a search, not a count."""
    return [c for c in CHAIN.findall(path.read_text(encoding="utf-8")) if ".parents" not in c]


def _resolve(chain: str, path: Path) -> Path:
    return eval(  # noqa: S307 - the only input is a regex over our own source
        chain, {"Path": Path, "__file__": str(path)}
    ).resolve()


@pytest.mark.parametrize("path", _tools(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_a_tool_never_climbs_to_a_directory_inside_tools(path: Path):
    """A tool resolving into ``tools/`` has the wrong number of ``parent`` hops.

    Siblings are named, never climbed to, so a ``Path(__file__)`` chain landing
    inside ``tools/`` is a miscount left over from the move. The tool would
    read ``tools/data/`` or ``tools/.env`` and report "not found" for data it
    actually has.
    """
    chains = _chains(path)
    if not chains:
        pytest.skip(f"{path.name} never derives a path from __file__")

    offenders = [c for c in chains if TOOLS_ROOT in _resolve(c, path).parents
                 or _resolve(c, path) == TOOLS_ROOT]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} climbs to {offenders}, which is inside "
        f"tools/ — one level short of the repository root ({REPO_ROOT})"
    )


@pytest.mark.parametrize("path", _tools(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_a_tool_never_escapes_the_repository(path: Path):
    """Climbing too far is as silent as climbing too little."""
    chains = _chains(path)
    if not chains:
        pytest.skip(f"{path.name} never derives a path from __file__")

    outside = [c for c in chains
               if _resolve(c, path) != REPO_ROOT
               and REPO_ROOT not in _resolve(c, path).parents]
    assert not outside, f"{path.relative_to(REPO_ROOT)} reaches outside the repo: {outside}"


def test_every_category_directory_exists():
    """The six categories are the whole point of the reorganisation."""
    for category in CATEGORIES:
        directory = TOOLS_ROOT / category
        assert directory.is_dir(), f"tools/{category} is missing"
        assert any(directory.iterdir()), f"tools/{category} is empty"


def test_the_legacy_subtree_moved_sideways_not_down():
    """``legacy/`` kept its depth, so its chains still resolve inside the repo."""
    if not LEGACY_ROOT.is_dir():
        pytest.skip("no legacy experiments to check")
    for path in sorted(LEGACY_ROOT.glob("*.py")):
        for chain in _chains(path):
            resolved = _resolve(chain, path)
            assert resolved == REPO_ROOT or REPO_ROOT in resolved.parents, (
                f"{path.name} climbs to {resolved}, outside the repository"
            )


def test_no_source_file_is_silently_untracked():
    """A file that exists on disk but not in git is missing from every clone.

    ``.gitignore`` said ``data/``. A gitignore pattern with a trailing slash
    and no leading one matches a directory of that name at *any* depth, so it
    also matched ``tools/data/`` — 13 operator scripts that were on disk, green
    in the test suite, and absent from every fresh checkout. No test fails
    when this happens; the symptom is a colleague asking where the script went.

    So the check is the blunt one: walk the shipped source trees, and a file is
    a bug only when it is on disk, *not* tracked, and *not* ignored — i.e. it
    fell through both nets. A file that is ignored on purpose is fine; a file
    git was never told about is a hole nobody can see.
    """
    import subprocess

    shipped = ["rpa", "tools", "tests", "apps/desktop/src", "apps/desktop/src-tauri/src"]
    lost: list[str] = []

    for tree in shipped:
        for path in sorted((REPO_ROOT / tree).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()

            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            if tracked.returncode == 0:
                continue  # in the index; nothing to report

            ignored = subprocess.run(
                ["git", "check-ignore", "-q", "--", relative],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            if ignored.returncode == 0:
                continue  # ignored deliberately

            lost.append(relative)

    assert not lost, (
        f"on disk, untracked, and not ignored: {lost}. A .gitignore rule without "
        f"a leading slash matches at every depth — check that a directory such as "
        f"tools/data/ is not being swallowed by a root-level rule."
    )


def test_the_runtime_data_rule_is_anchored_to_the_repository_root():
    """The one line that caused it, asserted directly.

    ``data/`` and ``/data/`` look the same in a diff and behave completely
    differently; this names the difference where someone will edit it next.
    """
    patterns = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "/data/" in patterns, (
        "the runtime-state rule must be written `/data/` so it cannot also match "
        "tools/data/"
    )
    assert "data/" not in patterns, (
        "a bare `data/` matches tools/data/ as well and hides 13 tracked scripts"
    )
