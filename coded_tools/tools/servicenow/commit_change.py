"""
Phase two of the approval gate: apply an approved change.

The token is read from sly_data and never from ``args``, because the framework
journals every argument verbatim into the chat stream — an approval carried in
``args`` would be published to the very model it exists to constrain.

The payload hash is recomputed from the arguments actually presented. If the model
altered the change after approval, or aimed it at a different record, the hashes
disagree and nothing is written.
"""

from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional

from coded_tools.tools.servicenow import gate
from coded_tools.tools.servicenow.base import ServiceNowTool
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.context import SLY_PROPOSAL_KEY
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import GateError
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.reporting import MARKER_CHANGE_VERIFIED
from coded_tools.tools.servicenow.reporting import report
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.transport import GatewayResult


class ServiceNowCommitChange(ServiceNowTool):
    """Apply a change that a human has approved."""

    OPERATION: str = "update"

    async def run(self, route: BoundRoute, args: Mapping[str, Any],
                  sly_data: Dict[str, Any], context: ToolContext,
                  profile: Profile) -> Dict[str, Any]:
        """
        :param route: The resolved update route.
        :param args: ``record`` and ``fields`` — which must match what was approved.
        :param sly_data: Supplies the commit token.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: The outcome of the write.
        :raises GateError: when the change was not approved as presented.
        """
        supplied: str = str(self.require(args, "record"))
        fields: Dict[str, Any] = self.writable_fields(route.entity, args.get("fields"))

        # Same grounding rule as the proposal: the identifier comes from the private
        # channel or a gateway lookup, never verbatim from the model. Since the
        # proposal recorded it, the usual path here is a private-channel hit with no
        # extra call.
        record_id, _ = await self.resolve_record(
            route.entity_name, route.entity, supplied, args, sly_data, context, profile,
            allow_lookup=False)

        token: Optional[str] = (sly_data or {}).get(SLY_COMMIT_TOKEN_KEY)
        verdict: gate.Verdict = gate.verify(profile, token, route.entity_name,
                                            record_id, fields, sly_data)

        if verdict.ok and verdict.result == "auto_approved" and record_id == supplied:
            # An auto-approved write has no proposal behind it, so nothing has
            # grounded the reference yet and no token pins the record. Ground it
            # now with a real lookup — otherwise a display number, or an invention,
            # would be written to the gateway as though it were an identifier.
            record_id, _ = await self.resolve_record(
                route.entity_name, route.entity, supplied, args, sly_data, context,
                profile, allow_lookup=True)

        await report(args, context, {
            MARKER_CHANGE_VERIFIED: verdict.ok,
            "record": record_id,
            "fields": sorted(fields),
            **verdict.as_dict(),
        }, content=("Approval verified." if verdict.ok
                    else f"Approval rejected: {verdict.reason}"))

        if not verdict.ok:
            raise GateError(
                f"This change has not been approved: {verdict.reason}. "
                "Propose the change first and have a person approve it.",
                reason=verdict.result, record=record_id)

        body: Dict[str, Any] = dict(fields)
        if not route.record_in_path:
            # Deployments differ on whether the identifier is addressed in the path
            # or carried in the body; the path template decides, so neither is assumed.
            body[route.entity.identifier_field] = record_id
        if profile.correlation_field:
            # Stamped by this package, not by the model, so it is not subject to the
            # writable-field allow-list. It is what joins the agent-side trail to the
            # system-of-record side.
            body[profile.correlation_field] = context.correlation_id

        result: GatewayResult = await self.gateway(profile).call(
            route.bind_record(record_id), args, context, body=body)

        # The approval is spent: clear it so a second call cannot reuse it even
        # within this request.
        sly_data.pop(SLY_COMMIT_TOKEN_KEY, None)
        sly_data.pop(SLY_PROPOSAL_KEY, None)

        return {
            "status": "committed",
            "record": record_id,
            "fields_changed": sorted(fields),
            "http_status": result.status,
            "result": result.body,
        }
