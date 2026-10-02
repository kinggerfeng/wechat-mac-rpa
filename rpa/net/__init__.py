"""The addresses this process can reach, and how a deployment changes them.

See :mod:`rpa.net.endpoints`. The short version: model endpoints are ours to
redirect, third-party endpoints are the user's, and both are enumerable in one
place so "where can this send my data" has an answer that is not a code read.
"""

from .endpoints import (
    ENDPOINTS,
    MODEL,
    THIRD_PARTY,
    Endpoint,
    EndpointDisabled,
    base_url,
    get,
    inventory,
    is_configured,
)

__all__ = [
    "ENDPOINTS", "MODEL", "THIRD_PARTY",
    "Endpoint", "EndpointDisabled",
    "base_url", "get", "inventory", "is_configured",
]
