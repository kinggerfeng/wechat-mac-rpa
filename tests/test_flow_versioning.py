"""A flow records which engine accepted it, and drift is visible.

An independent review of a comparable commercial RPA tool found the failure
this prevents: frequent releases, command behaviour shifting between versions,
existing flows needing patches to keep working. Nothing announced the change,
so the first sign was a run that behaved unlike it did the week before.

Seventy-four nodes make it a matter of when. Two identifiers answer two
different questions — a declared contract, and a digest of what is actually
registered, which catches the bump nobody remembered to make.
"""

from __future__ import annotations

import pytest

from apps.engine.registry import get_node_registry
from apps.engine.store import _registry_specs
from apps.engine.version import (
    ENGINE_VERSION,
    check,
    node_fingerprint,
    stamp,
)


@pytest.fixture(scope="module")
def fingerprint() -> str:
    return node_fingerprint(_registry_specs())


def test_a_saved_flow_carries_the_engine_that_accepted_it(tmp_path):
    from apps.engine.store import RpaStore

    store = RpaStore(tmp_path / "rpa.db")
    flow = store.save_flow("t", "probe", {"version": 1, "entry": "", "nodes": [], "edges": []})

    assert flow.graph["engine_version"] == ENGINE_VERSION
    assert flow.graph["node_fingerprint"]


def test_stamping_reaches_the_database_not_just_the_returned_dict(tmp_path, fingerprint):
    """The stamp has to survive a round trip, or it is decoration."""
    from apps.engine.store import RpaStore

    store = RpaStore(tmp_path / "rpa.db")
    store.save_flow("t", "probe", {"version": 1, "entry": "", "nodes": [], "edges": []})

    reloaded = store.get_flow("t")
    assert check(reloaded.graph, fingerprint)[0] == "MATCH"


def test_a_flow_saved_before_this_existed_is_unstamped_not_broken(fingerprint):
    """Reporting every legacy flow as incompatible would be a lie.

    They are simply unannotated, which is a third answer and not an error.
    """
    status, detail = check({"version": 1, "nodes": []}, fingerprint)
    assert status == "UNSTAMPED"
    assert detail


def test_drift_is_reported_and_named(fingerprint):
    status, detail = check(
        {"engine_version": ENGINE_VERSION, "node_fingerprint": "0000dead"}, fingerprint
    )
    assert status == "MISMATCH"
    assert "0000dead" in detail and fingerprint in detail


def test_a_flow_from_a_newer_engine_is_called_out_separately(fingerprint):
    """A downgrade is a different problem from a node change, and reads differently."""
    status, detail = check(
        {"engine_version": "99.0", "node_fingerprint": fingerprint}, fingerprint
    )
    assert status == "FUTURE"
    assert "99.0" in detail


def test_checking_never_raises(fingerprint):
    """This is reporting. A flow that cannot be described is still worth running."""
    for graph in ({}, {"engine_version": None}, {"node_fingerprint": None},
                  {"engine_version": "not-a-version"}):
        status, detail = check(graph, fingerprint)
        assert isinstance(status, str) and detail


def test_the_fingerprint_tracks_a_changed_parameter_set(fingerprint):
    """The derived half: a renamed parameter must change the digest.

    This is the case a hand-maintained version number misses — the change is
    real, it alters what an existing flow does, and nobody bumped anything.
    """
    real = _registry_specs()
    altered = dict(real)
    victim = sorted(real)[0]
    altered[victim] = [*real[victim], "parameter_added_since_the_flow_was_written"]

    assert node_fingerprint(altered) != fingerprint


def test_the_fingerprint_tracks_an_added_node(fingerprint):
    real = _registry_specs()
    assert node_fingerprint({**real, "a_node_that_did_not_exist": []}) != fingerprint


def test_the_fingerprint_ignores_prose_and_ordering(fingerprint):
    """Editing a label must not make every flow in the database look stale.

    Params are compared as sets for the same reason: the fingerprint answers
    "can these two engines run each other's flows", and reordering a list
    cannot change that.
    """
    real = _registry_specs()
    reordered = {k: list(reversed(v)) for k, v in real.items()}
    assert node_fingerprint(reordered) == fingerprint


def test_the_fingerprint_covers_every_registered_node():
    specs = _registry_specs()
    assert len(specs) >= 70, (
        f"only {len(specs)} nodes reached the fingerprint; the registry the "
        f"engine runs is not the one being hashed"
    )
    assert all(isinstance(v, list) for v in specs.values())


def _write_provenance(db_path, flow_id, engine_version, node_fingerprint):
    """Put a flow's provenance in as an *older or different* engine would have.

    ``save_flow`` always re-stamps with the running engine, so a stale row
    cannot be produced by saving one — it has to be written the way a previous
    release would have left it.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE flows SET engine_version=?, node_fingerprint=? WHERE id=?",
        (engine_version, node_fingerprint, flow_id),
    )
    conn.commit()
    conn.close()


def test_the_list_endpoint_reports_compatibility(tmp_path, monkeypatch):
    """The value is only useful if it reaches the person who can act on it.

    Reported in the flow list rather than only in a log, because the person who
    can fix a stale flow is the one looking at the list.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.company_api import rpa_api
    from apps.engine.store import RpaStore

    db_path = tmp_path / "rpa.db"
    store = RpaStore(db_path)
    store.save_flow("fresh", "当前引擎保存", {"version": 1, "entry": "", "nodes": [], "edges": []})
    store.save_flow("stale", "另一个引擎保存", {"version": 1, "entry": "", "nodes": [], "edges": []})
    _write_provenance(db_path, "stale", "1", "0000dead")

    monkeypatch.setattr(rpa_api, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(rpa_api.router)
    body = TestClient(app).get("/api/flows").json()

    assert body["engine_version"]
    by_id = {row["id"]: row for row in body["flows"]}
    assert by_id["fresh"]["engine_compat"]["status"] == "MATCH"
    assert by_id["stale"]["engine_compat"]["status"] == "MISMATCH"
    assert "0000dead" in by_id["stale"]["engine_compat"]["detail"]


def test_a_flow_from_before_versioning_is_reported_unstamped(tmp_path, monkeypatch):
    """A row with neither column predates versioning, and must not fail the list.

    The failure this guards is the one that makes a list unusable: one
    un-annotatable row takes the whole page down and the operator cannot see
    the other flows.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.company_api import rpa_api
    from apps.engine.store import RpaStore

    db_path = tmp_path / "rpa.db"
    store = RpaStore(db_path)
    store.save_flow("legacy", "版本标记之前保存", {"version": 1, "entry": "", "nodes": [], "edges": []})
    _write_provenance(db_path, "legacy", None, None)

    monkeypatch.setattr(rpa_api, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(rpa_api.router)
    response = TestClient(app).get("/api/flows")

    assert response.status_code == 200
    row = response.json()["flows"][0]
    assert row["engine_compat"]["status"] == "UNSTAMPED"
    assert row["engine_compat"]["detail"]


def test_stamp_is_idempotent(fingerprint):
    graph = {"version": 1}
    stamp(graph, fingerprint)
    once = dict(graph)
    stamp(graph, fingerprint)
    assert graph == once


def test_the_registry_the_fingerprint_reads_is_the_running_one():
    """The store must hash the registry it will actually execute against.

    A fingerprint computed from a test fixture proves nothing; this compares it
    against the live registry's own listing.
    """
    specs = _registry_specs()
    live = {spec.type: [p.name for p in spec.params]
            for spec in get_node_registry().list_specs()}
    assert specs == live
