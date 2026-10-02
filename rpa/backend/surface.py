"""Which parts of the desktop API a shipped build should carry.

Two surfaces exist in this repository and they are not the same product.

**Shipped** is the flow engine and the desktop shell: what an end user runs
their own automation on.

**Internal** is the quality loop — badcase review, ground-truth labelling,
benchmark reports, experiment comparison, code audit. It exists so the
project can tell whether a change made things better. It is a development
instrument, and shipping it to a customer tells them how the work is
measured, which is both noise and an invitation to argue with the numbers.

``rpa.backend.app`` used to mount every router unconditionally, so merely
importing the app pulled in four ``rpa.badcase`` modules. Splitting the
surfaces is what makes "build the product" different from "build the tool that
makes the product".
"""

from __future__ import annotations

#: Mounted in every build. The engine the customer actually runs.
SHIPPED_ROUTERS = ("rpa_api", "record_api")

#: Mounted only in a development build. Everything a customer does not need in
#: order to run their flows, and some of which they should not see.
INTERNAL_ROUTERS = ("cases_api",)


def should_mount(router_name: str, include_internal: bool) -> bool:
    if router_name in SHIPPED_ROUTERS:
        return True
    if router_name in INTERNAL_ROUTERS:
        return include_internal
    raise KeyError(f"未登记的 router: {router_name!r}")
