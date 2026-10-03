"""LLM provider persistence.

The behaviour that matters here is the api_key lifecycle: it must round-trip
for a client to be built, it must not be readable from a listing, and an edit
that does not resupply it must not destroy it. A settings page that silently
blanks a working key on save is worse than one that refuses to save.
"""

from __future__ import annotations


import pytest


from apps.engine.store import RpaStore


@pytest.fixture()
def store(tmp_path):
    return RpaStore(tmp_path / "rpa.db")


class TestProviderCrud:
    def test_insert_then_get(self, store):
        saved = store.save_provider({
            "name": "mimo", "base_url": "https://g/v1",
            "api_key": "sk-secret-value", "model": "mimo-v2.5",
        })
        assert saved["name"] == "mimo"
        got = store.get_provider(saved["id"])
        assert got["api_key"] == "sk-secret-value"
        assert got["model"] == "mimo-v2.5"

    def test_listing_masks_the_key(self, store):
        store.save_provider({
            "name": "mimo", "base_url": "https://g/v1",
            "api_key": "sk-secret-value", "model": "m",
        })
        listed = store.list_providers()
        assert len(listed) == 1
        assert listed[0]["api_key"] != "sk-secret-value"
        assert "sk-secret-value" not in str(listed[0])
        assert listed[0]["api_key_set"] is True

    def test_key_is_not_stored_in_the_clear(self, store, tmp_path):
        """Read the raw file: a dump must not hand over the key."""
        store.save_provider({
            "name": "n", "base_url": "https://g/v1",
            "api_key": "sk-secret-value", "model": "m",
        })
        raw = (tmp_path / "rpa.db").read_bytes()
        assert b"sk-secret-value" not in raw

    def test_edit_without_key_keeps_the_stored_one(self, store):
        """The UI submits a mask, not the key; saving must not blank it."""
        saved = store.save_provider({
            "name": "mimo", "base_url": "https://g/v1",
            "api_key": "sk-secret-value", "model": "mimo-v2.5",
        })
        store.save_provider({**saved, "name": "renamed", "api_key": ""})
        got = store.get_provider(saved["id"])
        assert got["api_key"] == "sk-secret-value"
        assert got["name"] == "renamed"

    def test_edit_with_new_key_replaces(self, store):
        saved = store.save_provider({
            "name": "n", "base_url": "https://g/v1",
            "api_key": "old-key-value", "model": "m",
        })
        store.save_provider({**saved, "api_key": "new-key-value"})
        assert store.get_provider(saved["id"])["api_key"] == "new-key-value"

    def test_delete(self, store):
        saved = store.save_provider({"name": "n", "base_url": "u", "model": "m"})
        assert store.delete_provider(saved["id"]) is True
        assert store.get_provider(saved["id"]) is None

    def test_missing_provider_is_none(self, store):
        assert store.get_provider("nope") is None


class TestDefaultProvider:
    def test_default_flag_is_exclusive(self, store):
        """Two defaults would make "the default" ambiguous at call time."""
        a = store.save_provider({"name": "a", "base_url": "u", "model": "m", "is_default": 1})
        b = store.save_provider({"name": "b", "base_url": "u", "model": "m", "is_default": 1})
        assert store.get_provider(a["id"])["is_default"] == 0
        assert store.get_provider(b["id"])["is_default"] == 1

    def test_default_provider_returns_the_flagged_row(self, store):
        store.save_provider({"name": "a", "base_url": "u", "model": "m"})
        wanted = store.save_provider(
            {"name": "b", "base_url": "u2", "model": "m2", "is_default": 1})
        assert store.default_provider()["id"] == wanted["id"]

    def test_default_falls_back_to_first_enabled(self, store):
        """A single-provider install should not need to tick a default box."""
        only = store.save_provider({"name": "only", "base_url": "u", "model": "m"})
        assert store.default_provider()["id"] == only["id"]

    def test_disabled_provider_is_not_default(self, store):
        store.save_provider({"name": "off", "base_url": "u", "model": "m",
                             "is_default": 1, "enabled": 0})
        assert store.default_provider() is None

    def test_no_providers_returns_none(self, store):
        """An install with no LLM configured is legitimate, not an error."""
        assert store.default_provider() is None

    def test_default_provider_returns_clear_key(self, store):
        store.save_provider({
            "name": "n", "base_url": "u", "model": "m",
            "api_key": "sk-secret-value", "is_default": 1,
        })
        assert store.default_provider()["api_key"] == "sk-secret-value"


class TestProviderHealth:
    def test_success_records_timestamp_and_clears_error(self, store):
        saved = store.save_provider({"name": "n", "base_url": "u", "model": "m"})
        store.mark_provider_result(saved["id"], ok=False, error="boom")
        assert store.get_provider(saved["id"])["last_error"] == "boom"
        store.mark_provider_result(saved["id"], ok=True)
        row = store.get_provider(saved["id"])
        assert row["last_error"] == ""
        assert row["last_ok_at"]

    def test_error_is_truncated(self, store):
        saved = store.save_provider({"name": "n", "base_url": "u", "model": "m"})
        store.mark_provider_result(saved["id"], ok=False, error="x" * 5000)
        assert len(store.get_provider(saved["id"])["last_error"]) <= 500


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestProviderBackedLLMNode:
    """The llm node must use a provider row without ever hard-coding a key."""

    def _flow(self, params):
        from apps.engine.schema import Flow

        return Flow(id="f_p", name="p", graph={
            "version": 1, "entry": "n1", "variables": {},
            "nodes": [{
                "id": "n1", "type": "llm", "name": "llm",
                "position": {"x": 0, "y": 0}, "params": params,
                "retry": {"max": 0, "delay": 0}, "timeout": None,
                "on_error": "fail",
                "outputs": ["text", "empty", "model"],
                "disabled": False, "path": None, "target": None,
            }],
            "edges": [],
        })

    def _run(self, params):
        import rpa.llm.openclaw_client as oc
        from apps.engine.executor import FlowExecutor

        seen: dict = {}

        class Fake:
            model = "from-provider"

            def chat(self, messages, temperature=None, max_tokens=None, timeout=None):
                seen.update(kwargs=seen.get("kwargs", {}), temperature=temperature,
                            max_tokens=max_tokens, timeout=timeout)
                return "ok"

        orig = oc.OpenClawClient
        oc.OpenClawClient = lambda **kw: (seen.update(client_kwargs=kw), Fake())[1]
        try:
            result = FlowExecutor().run(self._flow(params), "run_p")
        finally:
            oc.OpenClawClient = orig
        return result, seen

    def test_named_provider_supplies_credentials(self, store, monkeypatch):
        import apps.engine.store as store_mod
        monkeypatch.setattr(store_mod, "_store", store, raising=False)
        saved = store.save_provider({
            "name": "gw", "base_url": "https://gw.example/v1",
            "api_key": "sk-from-provider", "model": "mimo-v2.5",
            "is_default": 1,
        })
        result, seen = self._run({"prompt": "hi", "provider": saved["id"]})
        assert result.status == "ok", result.error
        assert seen["client_kwargs"]["base_url"] == "https://gw.example/v1"
        assert seen["client_kwargs"]["api_key"] == "sk-from-provider"
        assert seen["client_kwargs"]["model"] == "mimo-v2.5"

    def test_node_parameter_beats_provider(self, store, monkeypatch):
        import apps.engine.store as store_mod
        monkeypatch.setattr(store_mod, "_store", store, raising=False)
        saved = store.save_provider({
            "name": "gw", "base_url": "https://gw.example/v1",
            "api_key": "sk-from-provider", "model": "mimo-v2.5", "is_default": 1,
        })
        result, seen = self._run({
            "prompt": "hi", "provider": saved["id"],
            "base_url": "https://override/v1", "model": "override-model",
        })
        assert result.status == "ok", result.error
        assert seen["client_kwargs"]["base_url"] == "https://override/v1"
        assert seen["client_kwargs"]["model"] == "override-model"
        # The key still comes from the provider: the node has no key param.
        assert seen["client_kwargs"]["api_key"] == "sk-from-provider"

    def test_default_provider_used_when_node_names_none(self, store, monkeypatch):
        import apps.engine.store as store_mod
        monkeypatch.setattr(store_mod, "_store", store, raising=False)
        store.save_provider({
            "name": "gw", "base_url": "https://default.example/v1",
            "api_key": "sk-default", "model": "default-model", "is_default": 1,
        })
        result, seen = self._run({"prompt": "hi"})
        assert result.status == "ok", result.error
        assert seen["client_kwargs"]["base_url"] == "https://default.example/v1"

    def test_provider_sampling_defaults_are_inherited(self, store, monkeypatch):
        import apps.engine.store as store_mod
        monkeypatch.setattr(store_mod, "_store", store, raising=False)
        store.save_provider({
            "name": "gw", "base_url": "https://gw/v1", "api_key": "k",
            "model": "m", "temperature": 0.15, "max_tokens": 333, "is_default": 1,
        })
        result, seen = self._run({"prompt": "hi", "max_tokens": ""})
        assert result.status == "ok", result.error
        assert seen["temperature"] == 0.15
        assert seen["max_tokens"] == 333

    def test_unknown_provider_is_an_error(self, store, monkeypatch):
        """Silently calling a different gateway is the bug this prevents."""
        import apps.engine.store as store_mod
        monkeypatch.setattr(store_mod, "_store", store, raising=False)
        store.save_provider({"name": "gw", "base_url": "https://gw/v1",
                             "api_key": "k", "model": "m", "is_default": 1})
        result, _ = self._run({"prompt": "hi", "provider": "llmp_missing"})
        assert result.status == "error"
        assert "llmp_missing" in result.error
