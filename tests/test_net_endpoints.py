"""The egress registry is a control, not a list.

``rpa/net/endpoints.py`` claims two things a security review will test:

1. A deployment can point any address somewhere else with one variable.
2. A deployment can forbid an address entirely, and code that ignores that is
   a crash rather than a packet.

Both are testable, and neither is worth much without the test. The static scan
in ``test_network_egress.py`` proves the addresses live in one file; this file
proves that file can actually change where data goes and can actually stop it.
"""

from __future__ import annotations

import pytest

from rpa.net.endpoints import (
    ENDPOINTS,
    MODEL,
    THIRD_PARTY,
    EndpointDisabled,
    base_url,
    get,
    inventory,
    is_configured,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient variable may decide what these tests assert.

    A developer machine with ``OPENAI_BASE_URL`` exported would otherwise make
    the default-value test pass or fail depending on who ran it.
    """
    for endpoint in ENDPOINTS.values():
        monkeypatch.delenv(endpoint.env, raising=False)
        monkeypatch.delenv(endpoint.disable_env, raising=False)


def test_the_default_is_used_when_nothing_is_configured():
    assert base_url("dashscope") == ENDPOINTS["dashscope"].default


def test_one_variable_redirects_a_model_endpoint(monkeypatch):
    """The reseller case: point the product at a proxy you control.

    This is the requirement that made the registry worth building. Shipping an
    API key to a customer machine means the key is no longer the vendor's to
    rotate, so the base URL has to move without a code change.
    """
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://proxy.internal/v1")
    assert base_url("dashscope") == "https://proxy.internal/v1"


def test_a_trailing_slash_does_not_produce_a_double_slash(monkeypatch):
    """A gateway configured from a doc often ends in ``/``."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.corp/kimi/")
    assert base_url("kimi") == "https://gateway.corp/kimi"


def test_an_explicit_argument_beats_the_environment():
    """Per-call configuration must still win, or a vendor proxy is uncatchable."""
    assert base_url("deepseek", "https://one-off.local/v1") == "https://one-off.local/v1"


def test_a_disabled_endpoint_refuses_to_name_a_url(monkeypatch):
    monkeypatch.setenv("RPA_DISABLE_WTTR", "1")
    with pytest.raises(EndpointDisabled) as caught:
        base_url("wttr")
    assert "RPA_DISABLE_WTTR" in str(caught.value)


def test_a_disabled_endpoint_stays_disabled_despite_a_valid_url(monkeypatch):
    """An override must not be a way around the switch.

    Otherwise the control is advisory: an install that forbids egress but has a
    leftover ``RPA_WTTR_BASE_URL`` in its environment would still dial out.
    """
    monkeypatch.setenv("RPA_DISABLE_WTTR", "1")
    monkeypatch.setenv("RPA_WTTR_BASE_URL", "https://mirror.internal")
    with pytest.raises(EndpointDisabled):
        base_url("wttr", "https://mirror.internal")


def test_disabling_one_endpoint_leaves_the_others_working(monkeypatch):
    """A switch that takes the whole process down is not a switch."""
    monkeypatch.setenv("RPA_DISABLE_WTTR", "1")
    assert base_url("dashscope") == ENDPOINTS["dashscope"].default


def test_a_disabled_weather_lookup_never_dials(monkeypatch):
    """The property that matters, checked at a real call site.

    ``base_url`` raising is only half the claim. The other half is that no
    packet leaves — so the HTTP call is replaced with something that fails the
    test if it is ever reached.
    """
    from rpa.tools import builtin_tools

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("wttr.in was dialled despite RPA_DISABLE_WTTR")

    monkeypatch.setenv("RPA_DISABLE_WTTR", "1")
    monkeypatch.setattr(builtin_tools.requests, "get", _must_not_be_called)

    result = builtin_tools._get_weather(city="北京")

    assert "失败" in result, (
        f"expected the disabled endpoint to fail loudly, got {result!r} — "
        f"a silent empty result here would look like 'no weather data'"
    )


def test_the_inventory_is_what_a_security_review_would_ask_for():
    """Everything reachable, in one payload, with a reason for each.

    This exists so the question "where can this send my data" has an answer
    that is not a code read.
    """
    rows = inventory()
    assert [row["name"] for row in rows] == sorted(ENDPOINTS)
    for row in rows:
        assert row["category"] in {MODEL, THIRD_PARTY}
        assert row["what"], f"{row['name']} has no explanation"
        assert row["url"].startswith("https://")


def test_both_categories_are_present():
    """A registry that is all one kind means the classification never happened."""
    categories = {endpoint.category for endpoint in ENDPOINTS.values()}
    assert categories == {MODEL, THIRD_PARTY}


def test_an_unregistered_name_is_rejected_loudly():
    """A typo must not silently fall back to some other vendor's address."""
    with pytest.raises(KeyError) as caught:
        get("deepseek-v2")
    assert "endpoints.py" in str(caught.value)


def test_is_configured_is_not_the_same_as_enabled(monkeypatch):
    """A feature can be off because nobody pointed it, or because it is banned.

    The fact checker is the first case: the endpoint is fine, it just has not
    been given an address. Conflating the two would switch a default-off
    feature on for every deployment that merely has an API key set.
    """
    assert not is_configured("dashscope")
    assert get("dashscope").enabled()

    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://gateway.corp/v1")
    assert is_configured("dashscope")
    assert get("dashscope").enabled()

    monkeypatch.setenv("RPA_DISABLE_DASHSCOPE", "1")
    assert is_configured("dashscope")
    assert not get("dashscope").enabled()


def _generator(monkeypatch):
    from rpa.reply.generator import ReplyGenerator

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    gen = ReplyGenerator(llm_client=object())
    gen.enable_fact_check = True
    return gen


def test_fact_check_stays_off_until_a_base_url_is_named(monkeypatch):
    """The gate that used to read the variable directly now asks the registry.

    Behaviour is unchanged: an API key alone has never been enough to turn the
    fact checker on, and a migration that silently switched it on for every
    deployment would start spending tokens nobody agreed to spend.
    """
    import time

    gen = _generator(monkeypatch)
    assert gen._fact_check("证据", "回复", time.time() + 60) == ([], "")
    assert gen._fact_check_client is None, (
        "事实核查在没有显式配置 base_url 时被启用了"
    )


def test_fact_check_uses_the_registry_when_a_base_url_is_named(monkeypatch):
    import time

    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://gateway.corp/v1")
    gen = _generator(monkeypatch)
    gen._fact_check("证据", "回复", time.time() + 60)

    assert gen._fact_check_client is not None
    used = str(gen._fact_check_client.client.base_url)
    assert used.rstrip("/") == "https://gateway.corp/v1", (
        f"事实核查没有走注册表解析出的地址，实际用了 {used}"
    )


def test_disabling_dashscope_also_stops_the_fact_checker(monkeypatch):
    """The disable switch has to reach a feature that spends money.

    This is the test that would have caught a call site reading the variable
    behind the registry's back: the client would have been built anyway.
    """
    import time

    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://gateway.corp/v1")
    monkeypatch.setenv("RPA_DISABLE_DASHSCOPE", "1")
    gen = _generator(monkeypatch)

    assert gen._fact_check("证据", "回复", time.time() + 60) == ([], "")
    assert gen._fact_check_client is None, (
        "RPA_DISABLE_DASHSCOPE 没能阻止事实核查建立客户端"
    )
