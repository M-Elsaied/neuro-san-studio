"""
Phase one of the approval gate: propose a change, mutate nothing.

This tool reads the record, renders a real before/after diff, and mints a signed
commit token bound to the exact payload. The token goes onto sly_data and the diff
goes to the model — never the reverse. A model cannot approve its own change
because it never holds the thing that authorises one.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Mapping

from coded_tools.tools.servicenow import gate
from coded_tools.tools.servicenow import labels
from coded_tools.tools.servicenow.base import ServiceNowTool
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.context import SLY_PROPOSAL_KEY
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import ServiceNowError
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.reporting import MARKER_CHANGE_PROPOSED
from coded_tools.tools.servicenow.reporting import report
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.router import get_router


class ServiceNowProposeChange(ServiceNowTool):
    """Prepare a gated change and request human approval for it."""

    OPERATION: str = "update"

    async def run(self, route: BoundRoute, args: Mapping[str, Any],
                  sly_data: Dict[str, Any], context: ToolContext,
                  profile: Profile) -> Dict[str, Any]:
        """
        :param route: The resolved update route, used to validate writability.
        :param args: ``record`` (identifier) and ``fields`` (name/value pairs).
        :param sly_data: Receives the commit token; never returned to the model.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: The rendered diff and approval instructions.
        """
        supplied: str = str(self.require(args, "record"))
        fields: Dict[str, Any] = self.writable_fields(route.entity, args.get("fields"))

        if supplied.strip().lower() in gate.NEW_RECORD_ALIASES:
            # Proposing a record that does not exist yet. Resolving the create
            # route up front means "create is disabled here" surfaces now, at the
            # proposal, rather than after a human has already approved the change.
            get_router().resolve("create", route.entity_name)
            record_id = gate.NEW_RECORD
            current: Dict[str, Any] = {}
        else:
            # The identifier is resolved from the private channel or by a gateway
            # lookup — never taken verbatim from the model.
            record_id, current = await self.resolve_record(
                route.entity_name, route.entity, supplied, args, sly_data, context, profile)
            if current is None:
                current = await self.fetch_one(route.entity_name, record_id,
                                               args, context, profile)
            if current is None:
                raise ServiceNowError(
                    f"No record matching '{supplied}' was found, so there is nothing "
                    "to change.",
                    reason="record_not_found", record=supplied)

        creating: bool = record_id == gate.NEW_RECORD
        readable = set(route.entity.read_fields)
        diff: List[Dict[str, Any]] = [
            {
                "field": name,
                # Journal-style fields are frequently write-only; saying so beats
                # implying the current value is empty. Both sides are rendered as
                # labels so the person approving compares like with like.
                "before": ("<new record>" if creating
                           else labels.to_label(route.entity, name, current.get(name))
                           if name in readable else "<not readable>"),
                "after": labels.to_label(route.entity, name, value),
            }
            for name, value in fields.items()
        ]

        if gate.is_auto_approved(profile, fields):
            return {
                "status": "no_approval_required",
                "record": record_id,
                "diff": diff,
                "detail": "Every field in this change is on the deployment's "
                          "auto-approve list; commit may proceed directly.",
            }

        token, claims = gate.mint(profile, route.entity_name, record_id, fields,
                                  context.correlation_id)
        sly_data[SLY_COMMIT_TOKEN_KEY] = token
        sly_data[SLY_PROPOSAL_KEY] = {
            "record": record_id,
            # The reference as the caller gave it, so the commit turn can ground
            # the same reference even if the client returned only the proposal and
            # not the full record map.
            "reference": supplied,
            "entity": route.entity_name,
            "fields": sorted(fields),
            "payload_hash": claims["h"],
        }

        await report(args, context, {
            MARKER_CHANGE_PROPOSED: True,
            "record": record_id,
            # Names only. Values are the record's content and do not belong in an
            # audit marker unless a deployment explicitly allow-lists them.
            "fields": sorted(fields),
            "payload_hash": claims["h"],
            "token_id": claims["n"],
            "expires_at": claims["x"],
        }, content=f"Proposed a change to {record_id}; awaiting approval.")

        return {
            "status": "awaiting_approval",
            "record": record_id,
            "diff": diff,
            "expires_in_seconds": profile.gate.ttl_seconds,
            "instructions": "Present this diff to the person and ask them to approve it. "
                            "On approval, call the commit tool with exactly these same "
                            "record and fields values. The approval itself travels on the "
                            "private channel and is not available to you.",
        }
