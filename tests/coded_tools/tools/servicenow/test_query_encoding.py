"""
Wire-level query encoding on the production transport.

Live testing surfaced a gateway whose custom query parser rejects a
percent-escaped '=' inside sysparm_query (HTTP 500 on ``number%3DINC...``) but
accepts a literal one (HTTP 200 on ``number=INC...``). The stub server cannot
catch this: its compliant parser decodes %3D back to '=', so both forms look
identical to it. These tests therefore assert on the raw URL the transport hands
to ``requests`` — the only place the difference is visible — and pin the
``query_safe_chars`` profile knob that controls it.
"""

from typing import Any
from typing import Dict
from typing import List
from unittest import TestCase
from unittest import mock

from coded_tools.tools.servicenow.transport import RawResponse
from coded_tools.tools.servicenow.transport import RequestsTransport


class _CapturedRequest:
    """Stands in for requests.request, recording the URL it was called with."""

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

    @property
    def last_url(self) -> str:
        """:return: The URL of the most recent call."""
        return self.calls[-1]["url"]


class TestQuerySafeEncoding(TestCase):
    """The transport keeps declared characters literal in the query string."""

    def _send(self, query_safe: str) -> str:
        capture = _CapturedRequest()
        with mock.patch("coded_tools.tools.servicenow.transport.requests.request",
                        capture):
            RequestsTransport().request(
                "GET", "https://gw.example.com/read/incident",
                {"Accept": "application/json"},
                {"sysparm_display_value": "true", "sysparm_query": "number=INC001"},
                None, (5.0, 30.0), True, query_safe=query_safe)
        return capture.last_url

    def test_equals_stays_literal_when_declared_safe(self):
        url = self._send(query_safe="=")
        self.assertIn("sysparm_query=number=INC001", url)
        self.assertNotIn("%3D", url,
                         "a gateway with a custom parser rejects the escaped form")

    def test_params_still_present_and_joined(self):
        url = self._send(query_safe="=")
        self.assertIn("sysparm_display_value=true", url)
        self.assertIn("?", url)
        self.assertIn("&", url)

    def test_default_behaviour_escapes(self):
        # With no safe characters the transport leaves encoding to requests, so the
        # dict is passed through untouched (params=..., URL has no query yet).
        capture = _CapturedRequest()
        with mock.patch("coded_tools.tools.servicenow.transport.requests.request",
                        capture):
            RequestsTransport().request(
                "GET", "https://gw.example.com/read/incident",
                {"Accept": "application/json"},
                {"sysparm_query": "number=INC001"},
                None, (5.0, 30.0), True, query_safe="")
        self.assertEqual(capture.last_url, "https://gw.example.com/read/incident")
        self.assertEqual(capture.calls[-1]["params"], {"sysparm_query": "number=INC001"})

    def test_appends_with_ampersand_when_url_already_has_query(self):
        capture = _CapturedRequest()
        with mock.patch("coded_tools.tools.servicenow.transport.requests.request",
                        capture):
            RequestsTransport().request(
                "GET", "https://gw.example.com/read/incident?v=1",
                {"Accept": "application/json"},
                {"sysparm_query": "number=INC001"},
                None, (5.0, 30.0), True, query_safe="=")
        self.assertIn("?v=1&sysparm_query=number=INC001", capture.last_url)


class TestReturnsParsedResponse(TestCase):
    """A successful call still returns a RawResponse with parsed JSON."""

    def test_json_body_is_parsed(self):
        capture = _CapturedRequest()

        def _with_json(method: str, url: str, **kwargs: Any) -> Any:
            capture.calls.append({"method": method, "url": url, **kwargs})
            response = mock.Mock()
            response.status_code = 200
            response.content = b'{"result": []}'
            response.text = '{"result": []}'
            response.headers = {"Content-Type": "application/json"}
            response.json = mock.Mock(return_value={"result": []})
            response.request = mock.Mock(url=url)
            return response

        with mock.patch("coded_tools.tools.servicenow.transport.requests.request",
                        _with_json):
            result: RawResponse = RequestsTransport().request(
                "GET", "https://gw.example.com/read/incident",
                {"Accept": "application/json"},
                {"sysparm_query": "number=INC001"},
                None, (5.0, 30.0), True, query_safe="=")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.json_body, {"result": []})
