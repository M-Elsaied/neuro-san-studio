"""
End to end over real HTTP against the local stub gateway.

What this layer proves that unit tests cannot: the token exchange works over a
socket, query encoding and headers survive the wire, a 401 really does trigger a
re-mint and a retry, and the whole propose-approve-commit flow completes against a
server rather than an in-memory double.

No credentials, no network beyond loopback, no dependency to install.
"""

import json
import os
from typing import Any
from typing import Dict
from typing import Optional
from unittest import IsolatedAsyncioTestCase

import pytest

from coded_tools.tools.servicenow import profile as profile_module
from coded_tools.tools.servicenow import router as router_module
from coded_tools.tools.servicenow import transport as transport_module
from coded_tools.tools.servicenow.commit_change import ServiceNowCommitChange
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.profile import build_profile
from coded_tools.tools.servicenow.propose_change import ServiceNowProposeChange
from coded_tools.tools.servicenow.query_records import ServiceNowQueryRecords

from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import RecordingReporter
from coded_tools.tools.servicenow.demo.stub_gateway import ISSUED_TOKEN
from coded_tools.tools.servicenow.demo.stub_gateway import StubGateway

STUB_ENV: Dict[str, str] = {
    "SN_CLIENT_ID": "stub-client-id",
    "SN_CLIENT_SECRET": "stub-client-secret",
    "SN_GATE_KEYS": "stub-signing-key",
}


class TestAgainstStubServer(IsolatedAsyncioTestCase):
    """The real code path, over real HTTP."""

    stub: StubGateway

    @classmethod
    def setUpClass(cls) -> None:
        """
        Start one stub for the whole class.

        Deliberately not one per test. Starting and tearing down an HTTP listener
        between every test churns ephemeral ports, and a request can then reach a
        server that is still shutting down — which showed up as an intermittent 401
        in roughly one run in ten. One long-lived server with per-test state reset
        removes the race rather than narrowing it, and is faster besides.
        """
        cls.stub = StubGateway()
        cls.stub.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the shared stub."""
        cls.stub.__exit__(None, None, None)

    def setUp(self) -> None:
        self._saved: Dict[str, Optional[str]] = {}
        for name, value in STUB_ENV.items():
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        self.addCleanup(self._teardown)

        # Fresh synthetic state for each test, on the same listener.
        self.stub.state["records"] = {
            RECORD_ONE: {"sys_id": RECORD_ONE, "number": "REQ0001",
                         "short_description": "Sample request", "state": "1"},
        }
        self.stub.state["requests"] = []
        self.stub.state["writes"] = []

        profile_module.set_profile(build_profile(self.stub.profile_document()))
        router_module.reset_router()
        transport_module.reset_transport_state()
        self.reporter = RecordingReporter()

    def _teardown(self) -> None:
        for name, value in self._saved.items():
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
        :return: Framework-shaped arguments.
        """
        base: Dict[str, Any] = {"origin_str": "ServiceNowAssistant/StubTest",
                                "progress_reporter": self.reporter}
        base.update(extra)
        return base

    @pytest.mark.asyncio
    async def test_read_performs_a_real_token_exchange_then_reads(self):
        result = await self.read()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["records"][0]["number"], "REQ0001")

        paths = [item["path"] for item in self.stub.state["requests"]]
        self.assertIn("/oauth/token", paths)
        self.assertIn("/read/stub_request_table", paths)

    async def read(self, **extra: Any) -> Dict[str, Any]:
        """
        Perform a read and assert it succeeded.

        Checking the payload here rather than indexing into recorded requests means
        a failure reports the reason the tool gave, instead of an IndexError three
        lines later that says nothing about why.

        :param extra: Additional tool arguments.
        :return: The parsed result.
        """
        raw: str = await ServiceNowQueryRecords().async_invoke(
            self.args(operation="read", entity="request", **extra), {})
        result: Dict[str, Any] = json.loads(raw)
        self.assertNotIn("error", result,
                         f"The read failed: {result}. Stub saw: "
                         f"{[(item['method'], item['path']) for item in self.stub.state['requests']]}")
        return result

    def business_calls(self) -> list:
        """:return: Every non-token request the stub received."""
        return [item for item in self.stub.state["requests"]
                if item["path"].startswith("/read/")]

    @pytest.mark.asyncio
    async def test_bearer_token_is_actually_sent_on_the_business_call(self):
        await self.read()
        self.assertEqual(self.business_calls()[0]["headers"]["Authorization"],
                         f"Bearer {ISSUED_TOKEN}")

    @pytest.mark.asyncio
    async def test_token_is_minted_once_and_reused(self):
        for _ in range(3):
            await self.read()
        token_calls = [item for item in self.stub.state["requests"]
                       if item["path"] == "/oauth/token"]
        self.assertEqual(len(token_calls), 1,
                         "A warm token must be reused rather than reminted per call.")

    @pytest.mark.asyncio
    async def test_query_encoding_survives_the_wire(self):
        await self.read(query=f"sys_id={RECORD_ONE}")
        self.assertEqual(self.business_calls()[0]["params"]["sysparm_query"],
                         [f"sys_id={RECORD_ONE}"])

    @pytest.mark.asyncio
    async def test_full_propose_approve_commit_flow(self):
        sly: Dict[str, Any] = {}
        proposal = json.loads(await ServiceNowProposeChange().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "3"}), sly))
        self.assertEqual(proposal["status"], "awaiting_approval")
        self.assertEqual(self.stub.state["writes"], [])
        self.assertIn(SLY_COMMIT_TOKEN_KEY, sly)

        committed = json.loads(await ServiceNowCommitChange().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "3"}), sly))
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(len(self.stub.state["writes"]), 1)
        self.assertEqual(self.stub.state["records"][RECORD_ONE]["state"], "3")
        self.assertEqual(self.stub.state["writes"][0]["u_correlation"],
                         committed["correlation_id"])

    @pytest.mark.asyncio
    async def test_unapproved_commit_reaches_no_write_endpoint(self):
        result = json.loads(await ServiceNowCommitChange().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "9"}), {}))
        self.assertEqual(result["reason"], "missing_token")
        self.assertEqual(self.stub.state["writes"], [])
        self.assertEqual([item for item in self.stub.state["requests"]
                          if item["method"] == "PUT"], [])

    @pytest.mark.asyncio
    async def test_stale_token_triggers_a_remint_and_one_retry(self):
        # Warm the cache, then make the server reject that token: the tool should
        # mint again and succeed without the caller noticing.
        await self.read()
        # pylint: disable=protected-access
        transport_module.TOKEN_CACHE._token = type(
            transport_module.TOKEN_CACHE._token)(value="stale", expires_at=1e12)

        result = await self.read()
        self.assertEqual(result["count"], 1)
        token_calls = [item for item in self.stub.state["requests"]
                       if item["path"] == "/oauth/token"]
        self.assertEqual(len(token_calls), 2)
