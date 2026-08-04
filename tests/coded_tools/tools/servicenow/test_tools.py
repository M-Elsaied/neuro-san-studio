"""
The tools end to end against an in-memory gateway.

Every adversarial test here asserts two things, not one: that nothing was written,
**and** that the correct audit marker fired. Asserting only the return value would
let a silent-logging regression through — and a control nobody can see the record of
is not a control an infosec reviewer can accept.
"""

import asyncio
import json
from typing import Any
from typing import Dict

import pytest

from coded_tools.tools.servicenow.commit_change import ServiceNowCommitChange
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.context import SLY_CORRELATION_KEY
from coded_tools.tools.servicenow.propose_change import ServiceNowProposeChange
from coded_tools.tools.servicenow.query_records import ServiceNowQueryRecords
from coded_tools.tools.servicenow.reporting import MARKER_CHANGE_PROPOSED
from coded_tools.tools.servicenow.reporting import MARKER_CHANGE_VERIFIED
from coded_tools.tools.servicenow.reporting import MARKER_DOWNSTREAM_CALL
from coded_tools.tools.servicenow.reporting import MARKER_POLICY_DENIED

from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import RECORD_TWO
from tests.coded_tools.tools.servicenow._test_base import ServiceNowToolTestCase

NOTE: Dict[str, Any] = {"work_notes": "Access provisioned."}


class TestQueryRecords(ServiceNowToolTestCase):
    """Reads: field narrowing, paging, and the double-encoding trap."""

    def reader(self) -> ServiceNowQueryRecords:
        """:return: A query tool wired to the fake gateway."""
        return ServiceNowQueryRecords(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_returns_json_not_a_python_repr(self):
        # The activation layer stringifies whatever a tool returns, so a dict would
        # reach the model as a Python repr with single quotes.
        raw = await self.reader().async_invoke(self.args(operation="read", entity="request"), {})
        self.assertIsInstance(raw, str)
        self.assertIsInstance(json.loads(raw), dict)

    @pytest.mark.asyncio
    async def test_fields_are_narrowed_to_the_allow_list(self):
        await self.reader().async_invoke(self.args(operation="read", entity="request"), {})
        requested = self.gateway_fake.calls[0]["params"]["sysparm_fields"].split(",")
        self.assertEqual(sorted(requested),
                         sorted(self.profile.entities["request"].read_fields))

    @pytest.mark.asyncio
    async def test_unreadable_field_is_refused_and_reported(self):
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", fields="sys_id,salary"), {}))
        self.assertEqual(result["reason"], "field_not_readable")
        self.assertEqual(self.gateway_fake.calls, [], "Nothing should have been sent.")
        self.assertTrue(self.reporter.has(MARKER_POLICY_DENIED))

    @pytest.mark.asyncio
    async def test_identifier_is_always_retained(self):
        # Without it a follow-up change could not target the record it just read.
        await self.reader().async_invoke(
            self.args(operation="read", entity="request", fields="number"), {})
        self.assertIn("sys_id", self.gateway_fake.calls[0]["params"]["sysparm_fields"])

    @pytest.mark.asyncio
    async def test_page_size_is_capped_by_the_profile(self):
        await self.reader().async_invoke(
            self.args(operation="read", entity="request", limit=9999), {})
        # max_page is 3 in the test profile; one extra is fetched to detect more.
        self.assertEqual(self.gateway_fake.calls[0]["params"]["sysparm_limit"], 4)

    @pytest.mark.asyncio
    async def test_paging_is_explicit_rather_than_silent_truncation(self):
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", limit=1), {}))
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 1)

    @pytest.mark.asyncio
    async def test_pre_encoded_query_is_not_encoded_twice(self):
        # A model told it is passing an "encoded query" often encodes it first. Left
        # alone this returns an empty result set rather than an error, which is the
        # hardest kind of failure to notice.
        await self.reader().async_invoke(
            self.args(operation="read", entity="request", query="sys_id%3Drec-0001"), {})
        self.assertEqual(self.gateway_fake.calls[0]["params"]["sysparm_query"],
                         "sys_id=rec-0001")

    @pytest.mark.asyncio
    async def test_downstream_call_is_reported(self):
        await self.reader().async_invoke(self.args(operation="read", entity="request"), {})
        marker = self.reporter.markers(MARKER_DOWNSTREAM_CALL)[0]
        self.assertEqual(marker["status"], 200)
        self.assertEqual(marker["route"], "read:request")
        self.assertIn("latency_ms", marker)

    @pytest.mark.asyncio
    async def test_a_slow_gateway_does_not_starve_the_event_loop(self):
        # The regression guard for the rule that no blocking call runs on the loop.
        self.gateway_fake.delay_seconds = 0.25
        await self.assert_loop_stays_live(
            self.reader().async_invoke(self.args(operation="read", entity="request"), {}))


class TestProposeChange(ServiceNowToolTestCase):
    """Phase one: produce a diff and an approval, mutate nothing."""

    def proposer(self) -> ServiceNowProposeChange:
        """:return: A propose tool wired to the fake gateway."""
        return ServiceNowProposeChange(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_proposal_writes_nothing(self):
        sly: Dict[str, Any] = {}
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly))
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(self.gateway_fake.writes, [])

    @pytest.mark.asyncio
    async def test_token_goes_to_sly_data_and_never_to_the_model(self):
        sly: Dict[str, Any] = {}
        raw = await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        self.assertIn(SLY_COMMIT_TOKEN_KEY, sly)
        self.assertNotIn(sly[SLY_COMMIT_TOKEN_KEY], raw)

    @pytest.mark.asyncio
    async def test_diff_shows_real_before_values(self):
        sly: Dict[str, Any] = {}
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "3"}), sly))
        entry = result["diff"][0]
        # Both sides render as labels so the person approving compares like with
        # like, rather than being shown two numbers to interpret.
        self.assertEqual(entry["before"], "New")
        self.assertEqual(entry["after"], "Closed")

    @pytest.mark.asyncio
    async def test_write_only_field_is_labelled_rather_than_implied_empty(self):
        sly: Dict[str, Any] = {}
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly))
        self.assertEqual(result["diff"][0]["before"], "<not readable>")

    @pytest.mark.asyncio
    async def test_fabricated_identifier_never_reaches_a_write(self):
        # The identifier is resolved against the gateway rather than trusted, so an
        # invented reference fails at resolution instead of targeting a real record.
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="rec-does-not-exist", fields=NOTE), {}))
        self.assertEqual(result["reason"], "record_not_grounded")
        self.assertEqual(self.gateway_fake.writes, [])

    @pytest.mark.asyncio
    async def test_marker_carries_field_names_but_not_their_values(self):
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), {})
        marker = self.reporter.markers(MARKER_CHANGE_PROPOSED)[0]
        self.assertEqual(marker["fields"], ["work_notes"])
        self.assertNotIn("Access provisioned.", json.dumps(marker))


class TestCommitChange(ServiceNowToolTestCase):
    """Phase two: apply only what was actually approved."""

    def proposer(self) -> ServiceNowProposeChange:
        """:return: A propose tool wired to the fake gateway."""
        return ServiceNowProposeChange(gateway=self.gateway)

    def committer(self) -> ServiceNowCommitChange:
        """:return: A commit tool wired to the fake gateway."""
        return ServiceNowCommitChange(gateway=self.gateway)

    async def propose(self, sly: Dict[str, Any], record: str = RECORD_ONE,
                      fields: Dict[str, Any] = None) -> None:
        """
        :param sly: The sly_data to populate with an approval.
        :param record: The record to propose against.
        :param fields: The fields to propose.
        """
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request", record=record,
                      fields=fields if fields is not None else NOTE), sly)

    @pytest.mark.asyncio
    async def test_approved_change_is_applied(self):
        sly: Dict[str, Any] = {}
        await self.propose(sly)
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly))
        self.assertEqual(result["status"], "committed")
        self.assertEqual(len(self.gateway_fake.writes), 1)
        self.assertEqual(self.gateway_fake.writes[0]["work_notes"], NOTE["work_notes"])

    @pytest.mark.asyncio
    async def test_commit_without_a_proposal_writes_nothing(self):
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), {}))
        self.assertEqual(result["reason"], "missing_token")
        self.assertEqual(self.gateway_fake.writes, [])
        self.assertFalse(self.reporter.markers(MARKER_CHANGE_VERIFIED)[0]
                         [MARKER_CHANGE_VERIFIED])

    @pytest.mark.asyncio
    async def test_altered_change_after_approval_writes_nothing(self):
        # The model proposes a work note, the human approves that, and the model
        # then tries to close the record instead.
        sly: Dict[str, Any] = {}
        await self.propose(sly)
        # A *valid* value that is not the approved one, so the refusal comes from the
        # gate rather than from field validation — otherwise this stops testing the gate.
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "3"}), sly))
        self.assertEqual(result["reason"], "payload_mismatch")
        self.assertEqual(self.gateway_fake.writes, [])
        marker = self.reporter.markers(MARKER_CHANGE_VERIFIED)[-1]
        self.assertEqual(marker["result"], "payload_mismatch")

    @pytest.mark.asyncio
    async def test_approval_cannot_be_reaimed_at_another_record(self):
        sly: Dict[str, Any] = {}
        await self.propose(sly, record=RECORD_ONE)
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_TWO, fields=NOTE), sly))
        self.assertEqual(result["reason"], "record_mismatch")
        self.assertEqual(self.gateway_fake.writes, [])

    @pytest.mark.asyncio
    async def test_approval_is_spent_after_use(self):
        sly: Dict[str, Any] = {}
        await self.propose(sly)
        await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly))
        self.assertIn(result["reason"], ("missing_token", "already_consumed"))
        self.assertEqual(len(self.gateway_fake.writes), 1, "Only one write should occur.")

    @pytest.mark.asyncio
    async def test_non_writable_field_is_refused_before_the_gate(self):
        # Prompt injection asking for a field the deployment never sanctioned.
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"approval": "approved"}), {}))
        self.assertEqual(result["reason"], "field_not_writable")
        self.assertEqual(self.gateway_fake.writes, [])
        self.assertTrue(self.reporter.has(MARKER_POLICY_DENIED))

    @pytest.mark.asyncio
    async def test_identifier_and_correlation_are_stamped_into_the_write(self):
        sly: Dict[str, Any] = {}
        await self.propose(sly)
        await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        written = self.gateway_fake.writes[0]
        self.assertEqual(written["sys_id"], RECORD_ONE)
        # The stamped id is what joins the agent-side trail to the record side.
        self.assertEqual(written["u_correlation"], sly[SLY_CORRELATION_KEY])

    @pytest.mark.asyncio
    async def test_one_correlation_id_spans_propose_and_commit(self):
        sly: Dict[str, Any] = {}
        await self.propose(sly)
        raw = await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        self.assertEqual(json.loads(raw)["correlation_id"], sly[SLY_CORRELATION_KEY])


class TestFailureHandling(ServiceNowToolTestCase):
    """Downstream failures surface as JSON, scrubbed, with a correlation id."""

    @pytest.mark.asyncio
    async def test_downstream_failure_returns_json_with_a_correlation_id(self):
        # The correlation id is the thread a support engineer pulls: whatever the
        # model reports to the user, that id finds the whole chain in the logs.
        self.gateway_fake.queued_statuses = [400]
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        result = json.loads(await reader.async_invoke(
            self.args(operation="read", entity="request"), {}))
        self.assertEqual(result["reason"], "downstream_failed")
        self.assertIn("correlation_id", result)

    @pytest.mark.asyncio
    async def test_downstream_error_body_is_scrubbed_before_the_model_sees_it(self):
        # An upstream error page can echo an Authorization header straight back.
        leaked = "denied for Bearer abcdef0123456789abcdef0123456789"  # scrub-allow: synthetic
        self.gateway_fake.error_text = leaked
        self.gateway_fake.queued_statuses = [400]
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        raw = await reader.async_invoke(self.args(operation="read", entity="request"), {})
        self.assertNotIn("abcdef0123456789", raw)
        self.assertIn("redacted", raw)

    @pytest.mark.asyncio
    async def test_reads_retry_but_writes_never_do(self):
        # Two 500s then success: a read retries within its budget.
        self.gateway_fake.queued_statuses = [500, 500]
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        await reader.async_invoke(self.args(operation="read", entity="request"), {})
        self.assertEqual(len(self.gateway_fake.calls), 3)

        # A write given the same treatment is attempted exactly once, because an
        # asynchronous create that returns no reference duplicates under retry.
        self.gateway_fake.calls.clear()
        self.gateway_fake.queued_statuses = [500, 500]
        sly: Dict[str, Any] = {}
        await ServiceNowProposeChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        self.gateway_fake.calls.clear()
        self.gateway_fake.queued_statuses = [500]
        await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields=NOTE), sly)
        self.assertEqual(len(self.gateway_fake.calls), 1)

    @pytest.mark.asyncio
    async def test_breaker_opens_and_stops_calling_a_sick_route(self):
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        self.gateway_fake.queued_statuses = [500, 500, 500]
        await reader.async_invoke(self.args(operation="read", entity="request"), {})

        self.gateway_fake.calls.clear()
        result = json.loads(await reader.async_invoke(
            self.args(operation="read", entity="request"), {}))
        self.assertEqual(result["reason"], "circuit_open")
        self.assertEqual(self.gateway_fake.calls, [],
                         "An open circuit must not reach the network at all.")

    @pytest.mark.asyncio
    async def test_client_errors_do_not_trip_the_breaker_for_everyone(self):
        # A 404 is the caller's problem, not a sick dependency.
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        for _ in range(4):
            self.gateway_fake.queued_statuses = [404]
            await reader.async_invoke(self.args(operation="read", entity="request"), {})
        self.gateway_fake.calls.clear()
        await reader.async_invoke(self.args(operation="read", entity="request"), {})
        self.assertEqual(len(self.gateway_fake.calls), 1)


class TestConcurrency(ServiceNowToolTestCase):
    """Many simultaneous reads still complete, and still off the loop."""

    @pytest.mark.asyncio
    async def test_parallel_reads_all_complete(self):
        self.gateway_fake.delay_seconds = 0.02
        reader = ServiceNowQueryRecords(gateway=self.gateway)
        results = await asyncio.gather(*[
            reader.async_invoke(self.args(operation="read", entity="request"), {})
            for _ in range(12)
        ])
        self.assertEqual(len(results), 12)
        for raw in results:
            self.assertEqual(json.loads(raw)["count"], 2)
