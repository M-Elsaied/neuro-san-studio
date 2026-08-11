"""
Config-driven operations: an endpoint's shape and parameter set come from the
profile, and a gateway that accepts only a limited set is matched without code.

This is the regression home for the class of failure found in live testing: the
read tool sent parameters the gateway did not support and got an HTTP 500. The
fix — sending only the params an operation declares, and narrowing fields on the
response when the gateway cannot — is pinned here, including a fake that reproduces
the exact rejection.
"""

import json
from typing import Any
from typing import Dict

import pytest

from coded_tools.tools.servicenow import profile as profile_module
from coded_tools.tools.servicenow import router as router_module
from coded_tools.tools.servicenow import transport as transport_module
from coded_tools.tools.servicenow.query_records import ServiceNowQueryRecords
from coded_tools.tools.servicenow.router import Router

from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import ServiceNowToolTestCase
from tests.coded_tools.tools.servicenow._test_base import sample_document


class LimitedGatewayTestCase(ServiceNowToolTestCase):
    """
    A deployment whose read endpoint accepts only display + query — the shape of a
    curated wrapper rather than a full table API.
    """

    def setUp(self) -> None:
        super().setUp()
        document = sample_document()
        document["operations"]["read"] = {
            "method": "GET", "path": "/read/{table}", "shape": "query",
            "query_params": ["display", "query"],
        }
        profile = profile_module.build_profile(document)
        profile_module.set_profile(profile)
        router_module.reset_router()
        transport_module.reset_transport_state()
        self._prewarm_token()   # reset wiped the base class's warm token
        self.profile = profile
        # The fake now rejects anything beyond what a real limited gateway accepts.
        self.gateway_fake.allowed_params = ("sysparm_display_value", "sysparm_query")
        from coded_tools.tools.servicenow.transport import Gateway  # noqa: E402
        self.gateway = Gateway(profile, transport=self.gateway_fake)

    @staticmethod
    def _prewarm_token() -> None:
        """Seed the token cache so tests never reach a token endpoint."""
        import time  # noqa: E402  pylint: disable=import-outside-toplevel
        from coded_tools.tools.servicenow.auth import Token  # noqa: E402  pylint: disable=import-outside-toplevel
        # pylint: disable=protected-access
        transport_module.TOKEN_CACHE._token = Token(value="test-token",
                                                    expires_at=time.time() + 3600)
        transport_module.TOKEN_CACHE._style = "standard"

    def reader(self) -> ServiceNowQueryRecords:
        """:return: A query tool wired to the limited fake gateway."""
        return ServiceNowQueryRecords(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_only_declared_params_are_sent(self):
        await self.reader().async_invoke(
            self.args(operation="read", entity="request", query="number=REQ0001"), {})
        sent = set(self.gateway_fake.calls[0]["params"])
        self.assertEqual(sent, {"sysparm_display_value", "sysparm_query"},
                         "Only the operation's declared params may be sent.")

    @pytest.mark.asyncio
    async def test_read_succeeds_against_the_limited_gateway(self):
        # The exact scenario that returned 500 before the fix now returns records.
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", query=f"sys_id={RECORD_ONE}"), {}))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["count"], 1)

    @pytest.mark.asyncio
    async def test_field_allow_list_still_enforced_on_the_response(self):
        # sysparm_fields is not sent, so narrowing happens on the way back — the
        # returned record must carry only the entity's read_fields.
        self.gateway_fake.records[RECORD_ONE]["secret_field"] = "should-not-surface"
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", query=f"sys_id={RECORD_ONE}"), {}))
        returned = set(result["records"][0])
        self.assertNotIn("secret_field", returned)
        self.assertTrue(returned <= set(self.profile.entities["request"].read_fields))

    @pytest.mark.asyncio
    async def test_no_server_paging_reports_no_next_page(self):
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request"), {}))
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])

    @pytest.mark.asyncio
    async def test_the_fake_would_have_rejected_the_old_param_set(self):
        # Proves the reproduction is real: send the pre-fix param set directly and
        # confirm the limited gateway 500s it.
        raw = self.gateway_fake.request(
            "GET", "https://gateway.test/read/x",
            headers={}, params={"sysparm_display_value": "true",
                                "sysparm_exclude_reference_link": "true",
                                "sysparm_fields": "sys_id", "sysparm_limit": 2},
            json_body=None, timeout=(5, 5), verify=True)
        self.assertEqual(raw.status, 500)
        self.assertIn("sysparm_exclude_reference_link", raw.text)


class FullGatewayStillWorks(ServiceNowToolTestCase):
    """A gateway that supports the full set keeps server-side narrowing and paging."""

    @pytest.mark.asyncio
    async def test_full_param_set_is_sent_when_declared(self):
        # The default sample profile's read op declares the full set.
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        await reader.async_invoke(self.args(operation="read", entity="request"), {})
        sent = set(self.gateway_fake.calls[0]["params"])
        self.assertIn("sysparm_fields", sent)
        self.assertIn("sysparm_limit", sent)


class TestGatedDefaultsFromMethod(ServiceNowToolTestCase):
    """gated/retryable default from the HTTP method unless the profile overrides."""

    def test_get_is_ungated_and_retryable_by_default(self):
        route = Router(self.profile).resolve("read", "request")
        self.assertFalse(route.spec.gated)
        self.assertTrue(route.spec.retryable)

    def test_write_is_gated_and_not_retryable_by_default(self):
        route = Router(self.profile).resolve("update", "request")
        self.assertTrue(route.spec.gated)
        self.assertFalse(route.spec.retryable)

    def test_profile_can_override_gated(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["read"]["gated"] = True
        profile = profile_module.build_profile(document)
        self.assertTrue(Router(profile).resolve("read", "request").spec.gated)
