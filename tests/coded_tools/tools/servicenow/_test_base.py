"""
Shared scaffolding: a synthetic profile, an in-memory FakeGateway, and a progress
reporter that records what was emitted.

Modelled on the framework's own fully-mocked suites — a hand-written in-memory
stand-in injected by construction, no mocking library, no credentials, no network.
Every value here is synthetic and hand-written; nothing is recorded from a live
system, which is what lets the identifier-scrub test scan this directory too.
"""

import asyncio
import os
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple
from unittest import IsolatedAsyncioTestCase

from coded_tools.tools.servicenow import profile as profile_module
from coded_tools.tools.servicenow import router as router_module
from coded_tools.tools.servicenow import transport as transport_module
from coded_tools.tools.servicenow.auth import Token
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.profile import build_profile
from coded_tools.tools.servicenow.transport import Gateway
from coded_tools.tools.servicenow.transport import HttpTransport
from coded_tools.tools.servicenow.transport import RawResponse

GATE_KEY_ENV: str = "SN_TEST_GATE_KEYS"
GATE_KEY: str = "unit-test-signing-key-one"
ROTATED_KEY: str = "unit-test-signing-key-two"

RECORD_ONE: str = "rec-0001"
RECORD_TWO: str = "rec-0002"


def sample_document() -> Dict[str, Any]:
    """
    :return: A complete, valid profile document made of synthetic values.
    """
    return {
        "base_url": "https://gateway.test/api",
        "auth": {
            "style": "standard",
            "token_url": "https://gateway.test/oauth/token",
            "client_id_env": "SN_TEST_CLIENT_ID",
            "client_secret_env": "SN_TEST_CLIENT_SECRET",
        },
        "operations": {
            "read": {"method": "GET", "path": "/read/{table}"},
            "update": {"method": "PUT", "path": "/update/{table}"},
            "create": {"method": "POST", "path": "/create/{table}", "enabled": False},
        },
        "entities": {
            "request": {
                "table": "sample_request_table",
                "identifier_field": "sys_id",
                "display_field": "number",
                "read_fields": ["sys_id", "number", "short_description", "state"],
                "write_fields": ["work_notes", "state"],
                "coded_fields": {
                    "state": {"1": "New", "2": "In Progress", "3": "Closed"},
                },
            },
            "readonly": {
                "table": "sample_readonly_table",
                "read_fields": ["sys_id", "name"],
                "write_fields": [],
            },
        },
        "limits": {"default_page": 2, "max_page": 3, "max_retries": 2,
                   "backoff_base_seconds": 0.0, "breaker_threshold": 2,
                   "breaker_reset_seconds": 60},
        "gate": {"keys_env": GATE_KEY_ENV, "ttl_seconds": 300},
        "correlation_field": "u_correlation",
    }


def sample_profile(**overrides: Any) -> Profile:
    """
    :param overrides: Top-level keys to replace in the sample document.
    :return: A validated Profile built from synthetic values.
    """
    document: Dict[str, Any] = sample_document()
    document.update(overrides)
    return build_profile(document)


class RecordingReporter:
    """Stands in for the framework's AgentProgressReporter."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    async def async_report_progress(self, structure: Dict[str, Any], content: str = ""):
        """
        :param structure: The emitted event.
        :param content: The optional human-readable note.
        """
        self.events.append({"structure": structure, "content": content})

    def markers(self, marker: str) -> List[Dict[str, Any]]:
        """
        :param marker: The marker key to filter on.
        :return: Every emitted structure carrying that marker.
        """
        return [event["structure"] for event in self.events
                if marker in event["structure"]]

    def has(self, marker: str) -> bool:
        """
        :param marker: The marker key.
        :return: True when at least one event carried it.
        """
        return bool(self.markers(marker))


class FakeGateway(HttpTransport):
    """
    An in-memory stand-in for the gateway.

    Holds synthetic records, replays queued statuses, and records every call it
    received so a test can assert not just on the answer but on whether anything
    was written at all.
    """

    def __init__(self, records: Optional[Dict[str, Dict[str, Any]]] = None):
        self.records: Dict[str, Dict[str, Any]] = dict(records or {})
        self.calls: List[Dict[str, Any]] = []
        self.writes: List[Dict[str, Any]] = []
        #: Statuses to return instead of behaving normally, consumed in order.
        self.queued_statuses: List[int] = []
        #: Body returned with a queued error status, for scrubbing tests.
        self.error_text: str = "upstream failure"
        #: Seconds to block inside the (threaded) call, for loop-liveness testing.
        self.delay_seconds: float = 0.0
        self.fail_with: Optional[Exception] = None

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def request(self, method: str, url: str, headers: Mapping[str, str],
                params: Optional[Mapping[str, Any]], json_body: Optional[Mapping[str, Any]],
                timeout: Tuple[float, float], verify: bool) -> RawResponse:
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "body": dict(json_body or {}), "headers": dict(headers)})
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail_with is not None:
            raise self.fail_with
        if self.queued_statuses:
            status: int = self.queued_statuses.pop(0)
            if status >= 400:
                return RawResponse(status=status, text=self.error_text, json_body=None,
                                   headers={})
            return RawResponse(status=status, text="", json_body={"result": {}}, headers={})

        if method.upper() == "GET":
            return self._read(params or {})
        return self._write(json_body or {})

    def _read(self, params: Mapping[str, Any]) -> RawResponse:
        """
        :param params: The query parameters received.
        :return: Matching synthetic records.
        """
        query: str = str(params.get("sysparm_query", ""))
        matches: List[Dict[str, Any]] = list(self.records.values())
        if "=" in query:
            # Honour whichever field was queried. Hardcoding the identifier here
            # made the double more permissive than the real thing and hid lookups
            # by display value entirely.
            field, _, wanted = query.partition("=")
            matches = [record for record in matches if str(record.get(field)) == wanted]
        try:
            limit: int = int(params.get("sysparm_limit", len(matches)))
        except (TypeError, ValueError):
            limit = len(matches)
        try:
            offset: int = int(params.get("sysparm_offset", 0))
        except (TypeError, ValueError):
            offset = 0
        window: List[Dict[str, Any]] = matches[offset:offset + limit]
        return RawResponse(status=200, text="", json_body={"result": window}, headers={})

    def _write(self, body: Mapping[str, Any]) -> RawResponse:
        """
        :param body: The request body received.
        :return: A synthetic success response.
        """
        self.writes.append(dict(body))
        record_id: Optional[str] = body.get("sys_id")
        if record_id and record_id in self.records:
            self.records[record_id].update(
                {key: value for key, value in body.items() if key != "sys_id"})
        return RawResponse(status=200, text="",
                           json_body={"result": {"sys_id": record_id}}, headers={})


class ServiceNowToolTestCase(IsolatedAsyncioTestCase):
    """
    Shared setup: a synthetic profile installed process-wide, a fresh FakeGateway,
    a recording reporter, and a token cache pre-warmed so no test ever reaches a
    token endpoint.
    """

    def setUp(self) -> None:
        self.addCleanup(self._restore)
        self._saved_env: Dict[str, Optional[str]] = {}
        self._set_env(GATE_KEY_ENV, GATE_KEY)

        self.profile: Profile = sample_profile()
        profile_module.set_profile(self.profile)
        router_module.reset_router()
        transport_module.reset_transport_state()

        self.gateway_fake = FakeGateway(records={
            RECORD_ONE: {"sys_id": RECORD_ONE, "number": "REQ0001",
                         "short_description": "Sample request", "state": "1"},
            RECORD_TWO: {"sys_id": RECORD_TWO, "number": "REQ0002",
                         "short_description": "Another request", "state": "1"},
        })
        self.gateway = Gateway(self.profile, transport=self.gateway_fake)
        self.reporter = RecordingReporter()

        # Pre-warm the token cache: token acquisition has its own tests, and no
        # other test should depend on a token endpoint existing.
        # pylint: disable=protected-access
        transport_module.TOKEN_CACHE._token = Token(value="test-token",
                                                    expires_at=time.time() + 3600)
        transport_module.TOKEN_CACHE._style = "standard"

    def _set_env(self, name: str, value: Optional[str]) -> None:
        """
        :param name: Environment variable name.
        :param value: Value to set, or None to unset.
        """
        self._saved_env.setdefault(name, os.environ.get(name))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _restore(self) -> None:
        """Undo profile, router, transport and environment changes."""
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        profile_module.set_profile(None)
        router_module.reset_router()
        transport_module.reset_transport_state()

    def args(self, **extra: Any) -> Dict[str, Any]:
        """
        :param extra: Tool-specific arguments.
        :return: Arguments shaped the way the framework hands them to a CodedTool,
                 including the injected origin path and progress reporter.
        """
        base: Dict[str, Any] = {
            "origin_str": "ServiceNowAssistant/TestTool",
            "progress_reporter": self.reporter,
        }
        base.update(extra)
        return base

    async def assert_loop_stays_live(self, coroutine) -> Any:
        """
        Run a coroutine while a ticker also runs, and assert the ticker progressed.

        This is the regression guard for the rule that no blocking call may run on
        the event loop: if transport ever stopped using a worker thread, the ticker
        would be starved and this assertion would fail.

        :param coroutine: The coroutine under test.
        :return: Its result.
        """
        ticks: List[int] = []

        async def ticker() -> None:
            for index in range(200):
                ticks.append(index)
                await asyncio.sleep(0.001)

        ticker_task = asyncio.create_task(ticker())
        result: Any = await coroutine
        ticker_task.cancel()
        self.assertGreater(len(ticks), 1,
                           "The event loop was starved: a blocking call is running "
                           "on the loop instead of in a worker thread.")
        return result
