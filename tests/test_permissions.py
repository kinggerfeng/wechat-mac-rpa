"""The permission report must not contradict itself.

One response body, one question: which permissions are actually missing right
now. ``check_all`` answered it twice, with two rules, and the answers disagreed
in the same JSON. ``missing`` dropped ``not_determined`` on a shallow check,
because a permission macOS has not asked about yet is not a denial — and then
``_summary`` recomputed the list with plain ``not granted`` and counted it.
The page rendered the header count from one and the sentence from the other,
directly above the authoritative list, which matched the first.

Three states have to stay distinct, because collapsing any two of them is a
lie to whoever is trying to fix their permissions:

- **denied / unavailable** — something is in the way, name it
- **not_determined** — nobody has been asked, which is not the same as denied
- **granted** — measured, so it may be reported as granted

The tests drive ``_summary`` directly with synthesized statuses. The checks
themselves shell out to TCC, so a test that called ``check_all`` would either
prompt a real consent dialog or assert against whatever this machine happens
to have granted — neither is a test.
"""

from __future__ import annotations

import itertools

import pytest

from apps.engine.permissions import CHECK_KEYS, TITLES, _summary

GRANTED = "granted"
DENIED = "denied"
UNDETERMINED = "not_determined"
UNAVAILABLE = "unavailable"

#: The three states that can be combined across the three checked keys.
STATUSES = (GRANTED, DENIED, UNDETERMINED)


def _entry(key: str, status: str) -> dict[str, object]:
    """The same shape ``check_all`` hands to ``_summary``."""
    return {"key": key, "status": status, "granted": status == GRANTED}


def _report(statuses: dict[str, str], deep: bool = False) -> dict[str, object]:
    """Reproduce ``check_all``'s two derived fields for a given status map.

    Mirrors the production expression on purpose: the invariant under test is
    that ``summary`` agrees with ``missing``, so the recomputation has to be
    the real one rather than a re-derivation that could drift the same way.
    """
    entries = [_entry(key, statuses[key]) for key in CHECK_KEYS]
    missing = [
        e["key"]
        for e in entries
        if not e["granted"] and (deep or e["status"] != UNDETERMINED)
    ]
    return {"entries": entries, "missing": missing, "summary": _summary(entries, missing)}


#: Every reachable combination of the three checked keys, materialized because
#: pytest deprecates a generator here and the ids are worth keeping anyway.
COMBINATIONS = list(itertools.product(STATUSES, repeat=len(CHECK_KEYS)))


ALL_GRANTED = dict.fromkeys(CHECK_KEYS, GRANTED)


@pytest.mark.parametrize("combination", COMBINATIONS)
def test_the_summary_and_the_missing_list_never_contradict_each_other(combination: tuple[str, ...]):
    """The whole point, over every reachable combination of the three keys.

    A "缺少权限" summary is a rendering of ``missing``: the two must name
    exactly the same permissions. The other two branches of ``_summary`` talk
    about a different thing — one says nothing was denied, one says everything
    was measured — so they are held to their own rule below rather than to
    set equality with ``missing``.
    """
    statuses = dict(zip(CHECK_KEYS, combination, strict=True))
    for deep in (False, True):
        report = _report(statuses, deep=deep)
        summary = str(report["summary"])
        context = f"summary={summary!r} missing={report['missing']} deep={deep}"
        if summary.startswith("缺少权限："):
            named = {key for key in CHECK_KEYS if TITLES[key] in summary}
            assert named == set(report["missing"]), f"summary and missing disagree: {context}"
        elif report["missing"]:
            pytest.fail(f"missing is non-empty but the summary does not say so: {context}")


def test_a_denied_permission_is_named_and_the_rest_are_not():
    report = _report({**ALL_GRANTED, "screen_recording": DENIED, "automation": UNDETERMINED})
    assert report["missing"] == ["screen_recording"]
    assert report["summary"] == "缺少权限：屏幕录制"


def test_an_unavailable_dependency_is_a_real_blocker_not_a_quiet_one():
    report = _report({**ALL_GRANTED, "accessibility": UNAVAILABLE})
    assert report["missing"] == ["accessibility"]
    assert report["summary"] == "缺少权限：辅助功能"


def test_all_granted_says_so():
    assert _report(ALL_GRANTED)["summary"] == "全部权限已授予，可以运行自动化流程"


def test_nothing_denied_but_something_unasked_is_never_reported_as_granted():
    """``not_determined`` is not a denial — but reporting "all granted" about a
    state nobody measured is its own kind of wrong, and it is what a user acts
    on. The screen-recording case in the field report was exactly this."""
    report = _report({**ALL_GRANTED, "automation": UNDETERMINED})
    assert report["missing"] == []
    summary = str(report["summary"])
    assert "全部权限已授予" not in summary
    assert "尚未询问" in summary
    assert TITLES["automation"] in summary


def test_a_shallow_check_does_not_treat_unasked_as_a_blocker():
    """Refusing to start on a permission the system never prompted about would
    itself be a false report, so ``ok`` stays true. This is the behaviour that
    the contradicting summary quietly denied."""
    report = _report({**ALL_GRANTED, "automation": UNDETERMINED}, deep=False)
    assert report["missing"] == []
    assert not report["missing"]


def test_a_deep_check_promotes_unasked_to_a_blocker():
    """The Automation probe is the only way to learn whether it was granted,
    so once the user has asked for the deep check, silence must not read as a
    pass — and the summary has to agree with the promoted list."""
    report = _report({**ALL_GRANTED, "automation": UNDETERMINED}, deep=True)
    assert report["missing"] == ["automation"]
    assert report["summary"] == "缺少权限：自动化"
