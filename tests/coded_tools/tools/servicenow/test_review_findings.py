"""
Regression tests for the post-implementation review findings.

Each test here encodes a defect that shipped past the original 126-test suite, and
each was confirmed by reproduction before it was fixed. They are kept in one file,
named for what went wrong, so the next reviewer can see at a glance what this
package has already been wrong about:

  R1  The scrubber redacted the package's own identifiers — correlation id,
      payload hash, token id — out of every audit event. The join key of the whole
      audit design was being destroyed by our own control, and no test noticed
      because the marker assertions never checked those keys.
  R2  The grounding map was never released upstream, so it did not survive the
      client round-trip between the propose turn and the commit turn. A
      legitimately approved commit that referred to the record by its number
      failed as "approved for a different record".
  R3  The grounding rewrite made new-record proposals impossible: resolution
      demanded an existing record, so the creation gate could never be satisfied,
      even where create was enabled.
  R4  A model-supplied reference reached the grounding lookup unchecked, allowing
      encoded-query injection into the lookup (read-only blast radius, wrong on
      principle).
  R5  An auto-approved commit — which has no proposal and no token pinning —
      passed the supplied reference straight through as an identifier.
"""

import json
import uuid
from typing import Any
from typing import Dict

import pytest

from coded_tools.tools.servicenow import gate
from coded_tools.tools.servicenow.commit_change import ServiceNowCommitChange
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.context import SLY_CORRELATION_KEY
from coded_tools.tools.servicenow.context import SLY_PROPOSAL_KEY
from coded_tools.tools.servicenow.context import SLY_RECORD_MAP
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.create_record import ServiceNowCreateRecord
from coded_tools.tools.servicenow.propose_change import ServiceNowProposeChange
from coded_tools.tools.servicenow.query_records import ServiceNowQueryRecords
from coded_tools.tools.servicenow.reporting import build_event

from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import ServiceNowToolTestCase
from tests.coded_tools.tools.servicenow._test_base import sample_document
from tests.coded_tools.tools.servicenow._test_base import sample_profile

NOTE: Dict[str, Any] = {"work_notes": "Access provisioned."}

#: The keys allow.to_upstream releases — the ONLY sly_data a client ever sees and
#: can return. Round-trip tests must build their commit-turn sly_data from this
#: list and nothing else, or they test an in-process shortcut instead of the
#: two-turn reality (which is exactly the mistake that let R2 ship).
RELEASED_KEYS = (SLY_COMMIT_TOKEN_KEY, SLY_PROPOSAL_KEY, SLY_CORRELATION_KEY,
                 SLY_RECORD_MAP)


def round_tripped(sly: Dict[str, Any]) -> Dict[str, Any]:
    """
    :param sly: sly_data as it stands at the end of the propose turn.
    :return: What a fresh commit-turn request would actually receive.
    """
    return {key: sly[key] for key in RELEASED_KEYS if key in sly}


class TestR1AuditIdentifiersSurviveScrubbing(ServiceNowToolTestCase):
    """Our own identifiers must survive our own scrubber."""

    def test_correlation_id_survives_into_the_event(self):
        context = ToolContext(correlation_id=uuid.uuid4().hex, origin="Net/Tool",
                              tool="T", operation="update", entity="request")
        event = build_event(context, {"payload_hash": "abc123" * 11,
                                      "token_id": uuid.uuid4().hex})
        self.assertEqual(event["correlation_id"], context.correlation_id)
        self.assertNotIn("<redacted>", str(event["payload_hash"]))
        self.assertNotIn("<redacted>", str(event["token_id"]))

    def test_the_exemption_is_by_key_not_by_shape(self):
        # A secret of the same shape under any OTHER key must still be scrubbed —
        # otherwise the fix for R1 would have quietly disabled the scrubber.
        context = ToolContext(correlation_id="c-1", origin="Net/Tool", tool="T",
                              operation="read", entity="request")
        # scrub-allow: synthetic token, the input under redaction
        event = build_event(context, {"detail": "Bearer abcdef0123456789abcdef0123456789"})
        self.assertNotIn("abcdef0123456789", event["detail"])

    @pytest.mark.asyncio
    async def test_live_markers_carry_the_real_correlation_id(self):
        sly: Dict[str, Any] = {}
        await ServiceNowQueryRecords(gateway=self.gateway).async_invoke(
            self.args(operation="read", entity="request"), sly)
        marker = self.reporter.markers("servicenow_downstream_call")[0]
        self.assertEqual(marker["correlation_id"], sly[SLY_CORRELATION_KEY],
                         "The audit line must carry the id an investigator will "
                         "search for, not a redaction placeholder.")


class TestR2GroundingSurvivesTheClientRoundTrip(ServiceNowToolTestCase):
    """Propose and commit are separate requests; grounding must span them."""

    def proposer(self) -> ServiceNowProposeChange:
        """:return: A propose tool wired to the fake gateway."""
        return ServiceNowProposeChange(gateway=self.gateway)

    def committer(self) -> ServiceNowCommitChange:
        """:return: A commit tool wired to the fake gateway."""
        return ServiceNowCommitChange(gateway=self.gateway)

    @pytest.mark.asyncio
    async def test_commit_by_display_number_works_across_the_round_trip(self):
        sly: Dict[str, Any] = {}
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), sly)
        # Only the released keys come back — the two-turn reality.
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), round_tripped(sly)))
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.gateway_fake.writes[0]["sys_id"], RECORD_ONE)

    @pytest.mark.asyncio
    async def test_commit_still_works_when_only_token_and_proposal_return(self):
        # A minimal client that round-trips the token and proposal but drops the
        # record map: the proposal itself grounds the reference.
        sly: Dict[str, Any] = {}
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), sly)
        minimal = {key: sly[key] for key in (SLY_COMMIT_TOKEN_KEY, SLY_PROPOSAL_KEY)}
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), minimal))
        self.assertEqual(result["status"], "committed")

    @pytest.mark.asyncio
    async def test_a_reference_matching_nothing_still_fails_closed(self):
        sly: Dict[str, Any] = {}
        await self.proposer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), sly)
        result = json.loads(await self.committer().async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0002", fields=NOTE), round_tripped(sly)))
        self.assertEqual(result["reason"], "record_mismatch")
        self.assertEqual(self.gateway_fake.writes, [])


class TestR3NewRecordProposals(ServiceNowToolTestCase):
    """The creation gate must be satisfiable where create is enabled."""

    def setUp(self) -> None:
        super().setUp()
        document = sample_document()
        document["operations"]["create"]["enabled"] = True
        self.enable_profile(sample_profile(operations=document["operations"]))

    def enable_profile(self, profile) -> None:
        """
        :param profile: The profile to install for this test.
        """
        # pylint: disable=import-outside-toplevel
        from coded_tools.tools.servicenow import profile as profile_module
        from coded_tools.tools.servicenow import router as router_module
        from coded_tools.tools.servicenow.transport import Gateway
        profile_module.set_profile(profile)
        router_module.reset_router()
        self.profile = profile
        self.gateway = Gateway(profile, transport=self.gateway_fake)

    @pytest.mark.asyncio
    async def test_a_new_record_can_be_proposed_and_created(self):
        sly: Dict[str, Any] = {}
        proposal = json.loads(await ServiceNowProposeChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="new", fields=NOTE), sly))
        self.assertEqual(proposal["status"], "awaiting_approval")
        self.assertEqual(proposal["diff"][0]["before"], "<new record>")
        self.assertIn(SLY_COMMIT_TOKEN_KEY, sly)

        created = json.loads(await ServiceNowCreateRecord(gateway=self.gateway).async_invoke(
            self.args(operation="create", entity="request", fields=NOTE),
            round_tripped(sly)))
        self.assertEqual(created["status"], "created")
        self.assertEqual(len(self.gateway_fake.writes), 1)

    @pytest.mark.asyncio
    async def test_proposing_a_new_record_where_create_is_disabled_fails_at_proposal(self):
        # The refusal must land before a human is asked to approve something that
        # can never execute.
        self.enable_profile(sample_profile())  # create disabled again
        result = json.loads(await ServiceNowProposeChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="new", fields=NOTE), {}))
        self.assertEqual(result["reason"], "operation_disabled")
        self.assertEqual(self.gateway_fake.writes, [])


class TestR4LookupInjection(ServiceNowToolTestCase):
    """A reference is data, never query syntax."""

    @pytest.mark.asyncio
    async def test_query_syntax_in_a_reference_never_reaches_the_gateway(self):
        result = json.loads(await ServiceNowProposeChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001^state=1^ORDERBYDESCsys_id", fields=NOTE), {}))
        self.assertEqual(result["reason"], "record_not_grounded")
        self.assertEqual(self.gateway_fake.calls, [],
                         "An injected reference must be refused before any call.")


class TestR5AutoApprovedWritesAreGrounded(ServiceNowToolTestCase):
    """No proposal, no token pinning — so the lookup must do the grounding."""

    def setUp(self) -> None:
        super().setUp()
        document = sample_document()
        document["gate"]["auto_approve_fields"] = ["work_notes"]
        # pylint: disable=import-outside-toplevel
        from coded_tools.tools.servicenow import profile as profile_module
        from coded_tools.tools.servicenow import router as router_module
        from coded_tools.tools.servicenow.transport import Gateway
        profile = sample_profile(gate=document["gate"])
        profile_module.set_profile(profile)
        router_module.reset_router()
        self.profile = profile
        self.gateway = Gateway(profile, transport=self.gateway_fake)

    @pytest.mark.asyncio
    async def test_display_number_is_resolved_before_the_write(self):
        result = json.loads(await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ0001", fields=NOTE), {}))
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.gateway_fake.writes[0]["sys_id"], RECORD_ONE,
                         "The write must target the identifier, never the display "
                         "number passed through verbatim.")

    @pytest.mark.asyncio
    async def test_fabricated_reference_is_refused_even_when_auto_approved(self):
        result = json.loads(await ServiceNowCommitChange(gateway=self.gateway).async_invoke(
            self.args(operation="update", entity="request",
                      record="REQ9999", fields=NOTE), {}))
        self.assertEqual(result["reason"], "record_not_grounded")
        self.assertEqual(self.gateway_fake.writes, [])
        self.assertGreater(gate.is_auto_approved(self.profile, NOTE), 0,
                           "Sanity: the fields were auto-approvable; grounding is "
                           "what refused the write.")
