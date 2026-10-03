"""Every address this process can reach, in one place.

Not a style preference. This is the file an enterprise security review reads
when it asks where data goes, and it is the only place a vendor can redirect a
call without a code change.

The nine addresses this process used to hold were scattered across seven
modules and split into two kinds that need opposite treatment.

**Model endpoints** are ours to configure. ``base_url`` is what a reseller or
a self-hosted gateway substitutes, and the API key that goes with it belongs to
whoever ships the product — so a deployment must be able to point all three at
one proxy by setting three variables, not by patching three modules.

**Third-party endpoints** are the user's own data leaving to a service they
configured — a weather lookup, a stock quote, a smart-home control. They are
not ours to redirect, but an organisation that cannot permit them at all needs
a way to say so centrally, which scattered literals cannot offer.

The distinction is why this is a registry of addresses and not an HTTP client
wrapper. Wrapping every call would be a large refactor that changes nothing
about where data can go.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

MODEL = "model"
THIRD_PARTY = "third-party"


@dataclass(frozen=True)
class Endpoint:
    """One reachable address, and how a deployment may change it."""

    name: str
    category: str
    default: str
    env: str
    #: Set to a non-empty value to forbid this endpoint entirely, regardless of
    #: the URL in force. An air-gapped install has no business dialling a
    #: stock-quote service, and the way to say that is not a firewall rule
    #: someone has to remember.
    disable_env: str
    what: str

    def resolve(self, override: str | None = None) -> str:
        """The URL in force: explicit argument, then environment, then default."""
        if override:
            return override.rstrip("/")
        return os.environ.get(self.env, self.default).rstrip("/")

    def enabled(self) -> bool:
        return not os.environ.get(self.disable_env, "").strip()

    def configured(self) -> bool:
        """Whether a deployment overrode the default address.

        Distinct from :meth:`enabled`, and the distinction matters. A feature
        that is *off* until someone names a base URL — the fact checker is the
        one in this codebase — must ask this, not ``enabled``: the endpoint is
        perfectly enabled, it just has not been pointed anywhere yet.
        """
        return bool(os.environ.get(self.env, "").strip())

    def require_enabled(self) -> None:
        if not self.enabled():
            raise EndpointDisabled(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "url": self.resolve(),
            "env": self.env,
            "disable_env": self.disable_env,
            "what": self.what,
        }


class EndpointDisabled(RuntimeError):
    """Raised when a deployment has switched an endpoint off and code dialled it."""

    def __init__(self, endpoint: Endpoint) -> None:
        super().__init__(
            f"接口 {endpoint.name!r} 已被 {endpoint.disable_env} 禁用"
            f"（{endpoint.what}）"
        )
        self.endpoint = endpoint


ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint(
            name="dashscope",
            category=MODEL,
            default="https://dashscope.aliyuncs.com/compatible-mode/v1",
            env="DASHSCOPE_BASE_URL",
            disable_env="RPA_DISABLE_DASHSCOPE",
            what="通义千问视觉定位与 OCR，对应 apps/engine/vision.py 与 rpa/perception",
        ),
        Endpoint(
            name="kimi",
            category=MODEL,
            default="https://api.kimi.com/coding/v1",
            env="OPENAI_BASE_URL",
            disable_env="RPA_DISABLE_KIMI",
            what="Kimi 代理，回复生成主模型",
        ),
        Endpoint(
            name="deepseek",
            category=MODEL,
            default="https://api.deepseek.com/v1",
            env="LLM_BASE_URL",
            disable_env="RPA_DISABLE_DEEPSEEK",
            what="DeepSeek，回复生成备选模型",
        ),
        Endpoint(
            name="so",
            category=THIRD_PARTY,
            default="https://www.so.com",
            env="RPA_SO_BASE_URL",
            disable_env="RPA_DISABLE_SO",
            what="360 搜索，用户配置的浏览工具",
        ),
        Endpoint(
            name="wttr",
            category=THIRD_PARTY,
            default="https://wttr.in",
            env="RPA_WTTR_BASE_URL",
            disable_env="RPA_DISABLE_WTTR",
            what="天气查询",
        ),
        Endpoint(
            name="gtimg",
            category=THIRD_PARTY,
            default="https://qt.gtimg.cn",
            env="RPA_GTIMG_BASE_URL",
            disable_env="RPA_DISABLE_GTIMG",
            what="腾讯行情，股票报价",
        ),
        Endpoint(
            name="tuyacn",
            category=THIRD_PARTY,
            default="https://openapi.tuyacn.com",
            env="RPA_TUYACN_BASE_URL",
            disable_env="RPA_DISABLE_TUYACN",
            what="涂鸦智能家居",
        ),
    )
}


def get(name: str) -> Endpoint:
    try:
        return ENDPOINTS[name]
    except KeyError:
        raise KeyError(f"未登记的接口名 {name!r}；新增前先在 endpoints.py 登记并分类") from None


def is_configured(name: str) -> bool:
    """Whether a deployment pointed this endpoint somewhere non-default."""
    return get(name).configured()


def base_url(name: str, override: str | None = None) -> str:
    """The URL in force for a named endpoint, or refuse to name one.

    The disable check lives here rather than at each call site because a check
    someone has to remember adding is a check that will be missing on the one
    call site somebody adds next year. Every dial goes through this function, so
    ``RPA_DISABLE_*`` is enforceable from a single place.
    """
    endpoint = get(name)
    endpoint.require_enabled()
    return endpoint.resolve(override)


def inventory() -> list[dict[str, Any]]:
    """Everything this process can reach, for a security review or a bug report."""
    return [ENDPOINTS[name].to_dict() for name in sorted(ENDPOINTS)]
