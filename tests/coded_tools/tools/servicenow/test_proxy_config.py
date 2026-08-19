"""
The ServiceNow-scoped proxy.

The proxy exists so this package can reach the gateway through a corporate proxy
while an LLM in the same process reaches an internal endpoint directly. These tests
pin two things: the env-to-proxies mapping, and that the resolved proxies actually
reach the wire call (``requests``) — the whole point being that the gateway proxy
is applied without touching the process-global proxy the LLM uses.
"""

import os
from typing import Any
from typing import Dict
from typing import List
from unittest import TestCase
from unittest import mock

from coded_tools.tools.servicenow import netconfig
from coded_tools.tools.servicenow.transport import RequestsTransport


class ProxyEnvTestCase(TestCase):
    """Snapshot and restore the SN_* proxy env so tests never leak global state."""

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name)
                       for name in (netconfig.ENV_HTTPS_PROXY, netconfig.ENV_HTTP_PROXY)}
        for name in self._saved:
            os.environ.pop(name, None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class TestProxiesFromEnv(ProxyEnvTestCase):
    """SN_HTTPS_PROXY / SN_HTTP_PROXY map to a requests-style proxies dict."""

    def test_none_when_unset(self):
        self.assertIsNone(netconfig.proxies_from_env(),
                          "unset must defer to requests' global-env handling")

    def test_https_only_covers_http_too(self):
        os.environ[netconfig.ENV_HTTPS_PROXY] = "http://proxy.example:3128"
        self.assertEqual(netconfig.proxies_from_env(),
                         {"https": "http://proxy.example:3128",
                          "http": "http://proxy.example:3128"})

    def test_distinct_http_and_https(self):
        os.environ[netconfig.ENV_HTTPS_PROXY] = "http://secure.example:3129"
        os.environ[netconfig.ENV_HTTP_PROXY] = "http://plain.example:3128"
        self.assertEqual(netconfig.proxies_from_env(),
                         {"https": "http://secure.example:3129",
                          "http": "http://plain.example:3128"})

    def test_describe_hides_credentials(self):
        # scrub-allow: invented proxy URL with fake credentials, proves redaction
        described = netconfig.describe_proxies({"https": "http://user:secret@proxy.example:3128"})
        self.assertIn("proxy.example:3128", described)
        self.assertNotIn("secret", described)
        self.assertNotIn("user", described)

    def test_describe_none(self):
        self.assertIn("none", netconfig.describe_proxies(None))


class _CapturedRequest:
    """Stands in for requests.request, recording the kwargs it was called with."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = mock.Mock()
        response.status_code = 200
        response.content = b""
        response.text = ""
        response.headers = {}
        response.request = mock.Mock(url=url)
        return response


class TestTransportAppliesProxy(ProxyEnvTestCase):
    """The resolved proxy reaches the actual requests call — nothing else needed."""

    def _send(self) -> Dict[str, Any]:
        capture = _CapturedRequest()
        with mock.patch("coded_tools.tools.servicenow.transport.requests.request",
                        capture):
            RequestsTransport().request(
                "GET", "https://gw.example.com/read/incident",
                {"Accept": "application/json"}, {"sysparm_query": "number=X"},
                None, (5.0, 30.0), True)
        return capture.calls[-1]

    def test_proxy_passed_when_set(self):
        os.environ[netconfig.ENV_HTTPS_PROXY] = "http://proxy.example:3128"
        call = self._send()
        self.assertEqual(call["proxies"], {"https": "http://proxy.example:3128",
                                           "http": "http://proxy.example:3128"})

    def test_proxy_none_when_unset(self):
        call = self._send()
        self.assertIsNone(call["proxies"],
                          "unset SN_* proxy must pass None so requests uses global env")
