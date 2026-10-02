"""How many places can this process send data off the machine?

Not a style rule. This is the number an enterprise security questionnaire asks
for — "does my data leave the device, and to where" — and right now the honest
answer would require reading eight files and trusting the reader.

Three classes of outbound call, which need different treatment:

``model``       dashscope / kimi / deepseek. The API key belongs to whoever
                ships the product, so these must be able to point at a proxy
                the vendor controls without touching call sites.
``licensing``   licence checks and telemetry. Should be the *only* thing that
                phones home, and currently does not exist at all.
``third-party`` weather, stock quotes, smart-home control. The user configured
                these; the data is theirs and stays theirs.

The target is one egress module, not one because tidiness — because the first
question a security review asks cannot be answered by a grep today, and
because the vendor-proxy switch for model calls only becomes a one-line change
once every call site goes through one place.

Scope, stated so it is a decision and not an accident:

* Scanned: ``rpa/`` and the root entry scripts. That is what ships.
* Not scanned: ``tools/`` (one-off developer scripts, ~15 files, several of
  them stale ``legacy/`` experiments), ``third_party/reference/`` (vendored
  upstream source we do not ship), ``rpa/backend/`` (it dials the local API and
  nothing else). None of these reach the network in a user install; if that
  changes, this file is the place that says so.
* ``tests/`` is excluded for the same reason.

The baseline below is a ratchet, not a goal written down and forgotten. It can
only go down; raising it needs a deliberate edit to this file, which is a
reviewable act.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from rpa.net.endpoints import ENDPOINTS, THIRD_PARTY

REPO_ROOT = Path(__file__).resolve().parents[1]
RPA_ROOT = REPO_ROOT / "rpa"

#: The one file allowed to hold an address literal. Everything else routes
#: through it, which is what makes "where does my data go" a one-file answer.
EGRESS_REGISTRY = "rpa/net/endpoints.py"

#: Modules that legitimately talk to the local API and are not egress.
NOT_EGRESS = {("rpa", "backend")}

#: Every host this process may reach, with the class that decides how it is
#: treated. Cross-checked against the registry below, so the two cannot drift.
KNOWN_HOSTS = {
    "dashscope.aliyuncs.com": "model",
    "api.kimi.com": "model",
    "api.deepseek.com": "model",
    "www.so.com": THIRD_PARTY,
    "wttr.in": THIRD_PARTY,
    "qt.gtimg.cn": THIRD_PARTY,
    "openapi.tuyacn.com": THIRD_PARTY,
}

#: Ratchet. Lower it as call sites migrate; never raise it without a reason
#: written in the commit that raises it.
#: Now that all nine call sites go through the registry, the only remaining
#: literals *are* the registry: 7 defaults in 1 file. Previously 9 in 7.
BASELINE_EXTERNAL_URLS = 7
BASELINE_FILES_WITH_EGRESS = 1


def _scanned_files() -> list[Path]:
    """Every shipped Python file: the package plus the root entry scripts.

    Root-level scripts are here because ``run_bot.py`` held a real deepseek
    address that a package-only scan cannot see. A scan that only looks in
    ``rpa/`` is a scan with a hole shaped like the product's front door.
    """
    files = sorted(RPA_ROOT.rglob("*.py"))
    files += sorted(REPO_ROOT.glob("*.py"))
    return [
        p
        for p in files
        if tuple(p.relative_to(REPO_ROOT).parts[:2]) not in NOT_EGRESS
    ]


def _external_urls() -> dict[str, list[str]]:
    """Every non-local absolute URL literal, grouped by host.

    AST rather than grep: a host mentioned in a docstring is documentation,
    and a host in a string constant is something the process may dial.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for path in _scanned_files():
        relative = path.relative_to(REPO_ROOT)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if not value.startswith(("https://", "http://")):
                continue
            if "127.0.0.1" in value or "localhost" in value:
                continue
            host = value.split("//", 1)[1].split("/")[0]
            if not host:
                # A bare protocol, as in a URL parser's ("http://", "https://")
                # validation list. Nothing to dial, so not egress.
                continue
            found[host].append(relative.as_posix())
    return found


def test_the_egress_inventory_has_not_grown():
    urls = _external_urls()
    total = sum(len(v) for v in urls.values())
    assert total <= BASELINE_EXTERNAL_URLS, (
        f"{total} hardcoded external endpoints, up from the recorded "
        f"{BASELINE_EXTERNAL_URLS}. Every new one is another place data can "
        f"leave the machine and another place a security review has to look. "
        f"Route it through {EGRESS_REGISTRY} instead, or move the ratchet "
        f"deliberately."
    )


def test_the_egress_inventory_has_not_spread():
    files = {f for locations in _external_urls().values() for f in locations}
    assert len(files) <= BASELINE_FILES_WITH_EGRESS, (
        f"egress now spans {len(files)} files, up from {BASELINE_FILES_WITH_EGRESS}: "
        f"{sorted(files)}"
    )


def test_only_the_registry_holds_an_address():
    """The ratchet above bounds the count. This asserts where they live.

    A count of 7 is only meaningful if those 7 are the registry's defaults. If
    someone re-hardcodes a URL somewhere and deletes another, the count test
    still passes — this one does not.
    """
    holders = {
        f for locations in _external_urls().values() for f in locations
    }
    assert holders == {EGRESS_REGISTRY}, (
        f"addresses are held outside {EGRESS_REGISTRY}: {sorted(holders - {EGRESS_REGISTRY})}. "
        f"A scattered literal is one the vendor cannot redirect and one the "
        f"reviewer has to find by hand."
    )


def test_every_endpoint_is_classified():
    """An unclassified host cannot be reasoned about when the review comes.

    Classification is what decides whether a call can point at a vendor proxy
    or whether the user's own data goes to a third party on their behalf.
    """
    unknown = sorted(set(_external_urls()) - set(KNOWN_HOSTS))
    assert not unknown, (
        f"{unknown} reach the network and are not classified in KNOWN_HOSTS. "
        f"Add it with a category, or the egress module cannot route it."
    )


def test_the_registry_and_the_classification_agree():
    """Two lists of hosts drift unless something holds them together.

    ``KNOWN_HOSTS`` here and ``ENDPOINTS`` in the registry exist for different
    readers — this one for the guard, that one for the code. They are the same
    fact written twice, so the test is the tie.
    """
    registry = {}
    for endpoint in ENDPOINTS.values():
        host = endpoint.default.split("//", 1)[1].split("/")[0]
        registry[host] = endpoint.category
    assert registry == KNOWN_HOSTS, (
        f"registry says {registry}, KNOWN_HOSTS says {KNOWN_HOSTS}. "
        f"A host registered but unclassified cannot be routed; one classified "
        f"but unregistered cannot be dialled at all."
    )


def test_every_endpoint_can_be_centralisedly_disabled():
    """A registry entry with no way to forbid it is a comment, not a control."""
    for name, endpoint in ENDPOINTS.items():
        assert endpoint.disable_env, f"{name} has no disable_env"
        assert endpoint.env, f"{name} has no override env"
        assert endpoint.what, f"{name} does not say what it is for"


def test_the_inventory_is_non_empty_so_the_scan_is_real():
    """A guard over nothing is not a guard."""
    assert _external_urls(), (
        "no external URL literals found — the scan is broken, not the code clean"
    )


def test_model_hosts_are_the_ones_that_must_be_proxyable():
    """The vendor-proxy requirement is about credentials, not about privacy.

    Shipping an API key to a customer's machine means it is no longer a key
    the vendor controls. That is the one category where a call site must be
    able to change its base URL without changing anything else.
    """
    urls = _external_urls()
    model_hosts = {h for h, kind in KNOWN_HOSTS.items() if kind == "model" and h in urls}
    assert model_hosts, "no model host is present; the classification drifted"


def test_the_scan_actually_covers_the_root_entry_script():
    """Guards that silently stop covering code are worse than no guard.

    ``run_bot.py`` carried a deepseek address that a ``rpa/``-only scan could
    not see. This asserts the file is in scope, so a future refactor that
    changes the file layout cannot quietly re-open the hole.
    """
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _scanned_files()}
    assert "run_bot.py" in scanned, (
        f"root entry scripts are no longer scanned; scan covers {sorted(scanned)}"
    )


def test_endpoint_env_vars_are_read_only_in_the_registry():
    """Close the hole the literal scan cannot see.

    Re-hardcoding ``https://dashscope.aliyuncs.com/...`` at a call site is
    caught above, because the literal shows up. Reading the variable *without*
    a literal — ``os.environ.get("DASHSCOPE_BASE_URL", "")`` — leaves no trace
    for the scan, and the call site then quietly stops honouring a vendor proxy
    while still looking correct. That is the half-migrated parallel path this
    registry exists to prevent, so it is checked directly.
    """
    registered = {endpoint.env for endpoint in ENDPOINTS.values()}
    offenders: list[str] = []

    for path in _scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == EGRESS_REGISTRY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            is_env_read = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            first = node.args[0]
            if is_env_read and isinstance(first, ast.Constant) and first.value in registered:
                offenders.append(f"{relative}:{node.lineno} ({first.value})")

    assert not offenders, (
        f"endpoint variables read outside {EGRESS_REGISTRY}: {offenders}. "
        f"A call site that reads the variable itself does not get the disable "
        f"switch and does not survive a registry change — use "
        f"rpa.net.endpoints.base_url(name)."
    )

