"""
Two controls that exist because a language model will fill a gap rather than report one.

**Labels.** A coded value arriving without its meaning does not get reported as a
code — it gets a meaning invented for it. This was observed directly: the same
``state='1'`` was rendered "New" on one run and "Open" on the next, from identical
data. So codes are translated at the tool boundary, and an unmapped code is tagged
rather than passed through bare.

**Grounding.** An identifier the model hands back is not trusted. It must have been
produced by the gateway — read earlier in the request, or confirmed to exist now —
so a fabricated reference fails by name instead of targeting a real record.
"""

import json
from typing import Any
from typing import Dict

import pytest

from coded_tools.tools.servicenow import labels
from coded_tools.tools.servicenow.commit_change import ServiceNowCommitChange
from coded_tools.tools.servicenow.context import SLY_RECORD_MAP
from coded_tools.tools.servicenow.propose_change import ServiceNowProposeChange
from coded_tools.tools.servicenow.query_records import ServiceNowQueryRecords

from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import RECORD_TWO
from tests.coded_tools.tools.servicenow._test_base import ServiceNowToolTestCase

NOTE: Dict[str, Any] = {"work_notes": "Access provisioned."}


class TestLabelTranslation(ServiceNowToolTestCase):
    """Codes become labels on the way out, and labels become codes on the way in."""

    def reader(self) -> ServiceNowQueryRecords:
        """:return: A query tool wired to the fake gateway."""
        return ServiceNowQueryRecords(gateway=self.gateway)

    def proposer(self) -> ServiceNowProposeChange:
        """:return: A propose tool wired to the fake gateway."""
        return ServiceNowProposeChange(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_read_returns_labels_not_codes(self):
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request"), {}))
        states = {record["state"] for record in result["records"]}
        self.assertEqual(states, {"New"})
        self.assertNotIn("1", states, "A bare code is what the model invents meaning for.")

    @pytest.mark.asyncio
    async def test_unmapped_code_is_tagged_rather_than_passed_through_bare(self):
        # A code the deployment has not mapped must not look like ordinary data.
        self.gateway_fake.records[RECORD_ONE]["state"] = "9"
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", query=f"sys_id={RECORD_ONE}"), {}))
        self.assertEqual(result["records"][0]["state"], "<unmapped code 9>")

    @pytest.mark.asyncio
    async def test_uncoded_fields_are_left_alone(self):
        result = json.loads(await self.reader().async_invoke(
            self.args(operation="read", entity="request", query=f"sys_id={RECORD_ONE}"), {}))
        self.assertEqual(result["records"][0]["number"], "REQ0001")

    @pytest.mark.asyncio
    async def test_a_label_written_back_is_stored_as_its_code(self):
        # Without this, translating on read would silently break writes: the model
        # reads "Closed" and tries to write "Closed" into a field storing "3".
        sly: Dict[str, Any] = {}
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "Closed"}), sly)
        await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "Closed"}), sly)
        self.assertEqual(self.gateway_fake.writes[0]["state"], "3")

    @pytest.mark.asyncio
    async def test_code_and_label_normalise_to_the_same_approval(self):
        # The model may propose with a code and commit with a label. Both normalise
        # before hashing, so the signature still matches.
        sly: Dict[str, Any] = {}
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "3"}), sly)
        result = json.loads(await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "Closed"}), sly))
        self.assertEqual(result["status"], "committed")

    @pytest.mark.asyncio
    async def test_a_value_that_is_neither_code_nor_label_is_refused(self):
        # "Approved" sounds plausible and means nothing here. Refusing beats writing
        # an uninterpretable value into a live record.
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record=RECORD_ONE, fields={"state": "Approved"}), {}))
        self.assertEqual(result["reason"], "unrecognised_value")
        self.assertEqual(self.gateway_fake.writes, [])

    def test_translation_round_trips(self):
        entity = self.profile.entities["request"]
        for code, label in (("1", "New"), ("2", "In Progress"), ("3", "Closed")):
            self.assertEqual(labels.to_label(entity, "state", code), label)
            self.assertEqual(labels.to_code(entity, "state", label), code)
            self.assertEqual(labels.to_code(entity, "state", code), code)

    def test_label_matching_ignores_case(self):
        entity = self.profile.entities["request"]
        self.assertEqual(labels.to_code(entity, "state", "in progress"), "2")


class TestIdentifierGrounding(ServiceNowToolTestCase):
    """An identifier reaching a write must have come from the gateway."""

    def reader(self) -> ServiceNowQueryRecords:
        """:return: A query tool wired to the fake gateway."""
        return ServiceNowQueryRecords(gateway=self.gateway)

    def proposer(self) -> ServiceNowProposeChange:
        """:return: A propose tool wired to the fake gateway."""
        return ServiceNowProposeChange(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_a_read_records_identifiers_on_the_private_channel(self):
        sly: Dict[str, Any] = {}
        await self.reader().async_invoke(self.args(operation="read", entity="request"), sly)
        mapping = sly[SLY_RECORD_MAP]["request"]
        # Reachable by the human-meaningful number and by the identifier itself.
        self.assertEqual(mapping["REQ0001"], RECORD_ONE)
        self.assertEqual(mapping[RECORD_ONE], RECORD_ONE)

    @pytest.mark.asyncio
    async def test_a_display_number_resolves_to_the_identifier(self):
        sly: Dict[str, Any] = {}
        await self.reader().async_invoke(self.args(operation="read", entity="request"), sly)
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), sly))
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["record"], RECORD_ONE,
                         "The write must target the identifier, not the display value.")

    @pytest.mark.asyncio
    async def test_resolution_from_the_private_channel_costs_no_extra_call(self):
        sly: Dict[str, Any] = {}
        await self.reader().async_invoke(self.args(operation="read", entity="request"), sly)
        before = len(self.gateway_fake.calls)
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), sly)
        # One call: the fetch that builds the diff. No lookup, because the read
        # already grounded the identifier.
        self.assertEqual(len(self.gateway_fake.calls) - before, 1)

    @pytest.mark.asyncio
    async def test_an_unread_but_real_number_is_looked_up(self):
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0002", fields=NOTE), {}))
        self.assertEqual(result["record"], RECORD_TWO)

    @pytest.mark.asyncio
    async def test_a_fabricated_reference_is_refused_by_name(self):
        result = json.loads(await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ9999", fields=NOTE), {}))
        self.assertEqual(result["reason"], "record_not_grounded")
        self.assertEqual(self.gateway_fake.writes, [])

    @pytest.mark.asyncio
    async def test_commit_does_not_look_up_and_says_not_approved(self):
        # A legitimate commit always follows a proposal, which recorded the
        # identifier. A miss therefore means no approval exists — and saying so is
        # more use to the caller than a lookup would be.
        before = len(self.gateway_fake.calls)
        result = json.loads(await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), {}))
        self.assertEqual(result["reason"], "missing_token")
        self.assertEqual(self.gateway_fake.writes, [])
        self.assertEqual(len(self.gateway_fake.calls), before,
                         "An unapproved commit must not touch the gateway at all.")
