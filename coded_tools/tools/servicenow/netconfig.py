"""
ServiceNow-scoped HTTP proxy.

The neuro-san process makes two kinds of outbound call with opposite network
needs in one process: an LLM that must reach an internal endpoint **directly**
(no proxy), and this package, which must reach the ServiceNow gateway **through**
a corporate proxy. A single process-global ``HTTP_PROXY``/``HTTPS_PROXY`` cannot
serve both — setting it for the gateway also routes the LLM through the proxy,
where it fails (TLS interception with an untrusted CA, or no route).

So the proxy is scoped here: ``SN_HTTPS_PROXY`` / ``SN_HTTP_PROXY`` apply to
ServiceNow's own ``requests`` calls only. Leave the global proxy empty and the LLM
keeps working directly. The proxy stays an environment variable, not a profile
field, by deliberate decision — it is a property of where the process runs, not of
the gateway contract.

When neither variable is set, ``proxies_from_env`` returns ``None`` and ``requests``
falls back to its usual global-env behaviour, so nothing changes for a deployment
that has always relied on the global proxy.
"""

import os
from typing import Dict
from typing import Optional

ENV_HTTPS_PROXY: str = "SN_HTTPS_PROXY"
ENV_HTTP_PROXY: str = "SN_HTTP_PROXY"


def proxies_from_env() -> Optional[Dict[str, str]]:
    """
    :return: A ``requests``-style proxies map built from the SN_* proxy variables,
             or None when neither is set (defer to requests' global-env handling).
    """
    https: Optional[str] = os.environ.get(ENV_HTTPS_PROXY)
    http: Optional[str] = os.environ.get(ENV_HTTP_PROXY)
    if not https and not http:
        return None
    proxies: Dict[str, str] = {}
    if https:
        proxies["https"] = https
    if http or https:
        # An https-only setting still covers an http hop to the same proxy.
        proxies["http"] = http or https
    return proxies


def describe_proxies(proxies: Optional[Dict[str, str]]) -> str:
    """
    :param proxies: The proxies map, or None.
    :return: A host-only description safe to log — a proxy URL may embed
             ``user:pass@``, which is never emitted.
    """
    if not proxies:
        return "none (global env / direct)"
    parts = []
    for scheme, url in sorted(proxies.items()):
        host = url.split("://", 1)[-1].rsplit("@", 1)[-1]
        parts.append(f"{scheme}={host}")
    return ", ".join(parts)
