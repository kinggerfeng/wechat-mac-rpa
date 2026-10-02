"""Test configuration.

This file is the reason 25 test modules no longer each open with their own
``sys.path.insert(0, str(Path(__file__).resolve().parents[2]))``. pytest loads
``conftest.py`` from the rootdir *before* collecting anything, so putting the
repository root on ``sys.path`` once here is both less code and strictly more
reliable — a test file copied elsewhere by hand still imports correctly.

Layout
------
``tests/``          the suite that runs in CI. Pure and near-pure; anything
                    here that needs a real window or a live server is a bug.
``tests/e2e/``      needs a real WeChat window and/or a running 8768. Collected
                    by CI to prove it still *imports*, never executed there.
``tests/fixtures/`` checked-in sample data. Runtime-generated caches under
                    here are gitignored, not the files themselves.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Repository root: ``<root>/tests/conftest.py`` → ``<root>``.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The fixtures directory, for tests that need a path rather than an import.
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption("--run-api", action="store_true", default=False, help="Run with real API calls")
    parser.addoption("--n-runs", type=int, default=3, help="Number of runs per case")
