"""The shipped build is a decision, not an accident.

``rpa.backend.app`` used to mount every router unconditionally, so importing
it pulled four ``rpa.badcase`` modules into the process whether or not the
customer's build needed them. The quality loop — badcase review, ground-truth
labelling, benchmark reports, experiment comparison, code audit — is how the
project measures itself, and shipping it to a customer hands them the scoring
rubric as well as the tool.

The flow engine stays in both. The point is that what a customer runs and what
the team uses to improve it are separable, and the separation is explicit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import sys
sys.path.insert(0, {root!r})
import rpa.backend.app as m
paths = set(m.app.openapi()["paths"])
print(len(paths))
print(len([p for p in paths if p.startswith("/api/cases")]))
print(len([p for p in paths if "/flows" in p]))
print(len([k for k in sys.modules if k.startswith("rpa.badcase")]))
"""


def _probe(include_internal: bool) -> tuple[int, int, int, int]:
    """A fresh interpreter, because the surface is chosen at import time."""
    env = {"RPA_DESKTOP_INTERNAL": "1" if include_internal else "0"}
    import os

    full_env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(root=str(REPO_ROOT))],
        capture_output=True, text=True, env=full_env, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return tuple(int(line) for line in result.stdout.split())  # type: ignore[return-value]


def test_the_two_surfaces_are_different_sizes():
    with_internal = _probe(True)
    without_internal = _probe(False)
    assert with_internal[0] > without_internal[0], (
        "disabling the internal surface changed nothing; the switch is not wired"
    )


def test_a_shipped_build_carries_no_cases_routes():
    _, cases, _, badcase = _probe(False)
    assert cases == 0, "a shipped build still exposes /api/cases"
    assert badcase == 0, (
        "importing the shipped app pulled in rpa.badcase; the quality loop is "
        "an internal instrument and is reachable by the customer"
    )


def test_a_shipped_build_still_carries_the_flow_engine():
    """The whole point is to drop the instruments, not the product."""
    _, _, flows, _ = _probe(False)
    assert flows > 0, "the shipped build lost its flow routes"


def test_a_development_build_keeps_everything():
    total, cases, flows, badcase = _probe(True)
    assert total > 0 and cases > 0 and flows > 0
    assert badcase > 0, "the development build lost the quality tooling"


def test_the_flag_is_documented_where_it_is_read():
    text = (REPO_ROOT / "rpa" / "backend" / "app.py").read_text(encoding="utf-8")
    assert "RPA_DESKTOP_INTERNAL" in text
    surface = (REPO_ROOT / "rpa" / "backend" / "surface.py").read_text(encoding="utf-8")
    assert "RPA_DESKTOP_INTERNAL" not in surface or "INCLUDE_INTERNAL" in surface


def test_every_router_is_registered_in_one_of_the_two_lists():
    """An unregistered router fails loudly rather than defaulting to shipped.

    Adding a router and forgetting to classify it is the exact way internal
    tooling ends up in a customer build again.
    """
    from rpa.backend.surface import INTERNAL_ROUTERS, SHIPPED_ROUTERS, should_mount

    assert set(SHIPPED_ROUTERS) & set(INTERNAL_ROUTERS) == set()
    for name in (*SHIPPED_ROUTERS, *INTERNAL_ROUTERS):
        assert should_mount(name, True) is True
    for name in INTERNAL_ROUTERS:
        assert should_mount(name, False) is False
    with pytest.raises(KeyError):
        should_mount("a_router_nobody_classified", True)
