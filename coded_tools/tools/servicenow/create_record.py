"""
Create a record.

Ships **disabled**. No create endpoint has been confirmed for the deployment this
package was written against, and a fabricated path is worse than a missing one: the
plausible failure is not a clean 404 but a silent insert against a live
system-of-record. Enabling it is a profile change once the endpoint is confirmed in
writing.

Creation is also asynchronous on gateways of this shape — a ``202`` with no
reliable reference. That is why nothing here retries and why the correlation id is
stamped into the payload: confirmation is done by looking for that id, not by
resending and hoping.
"""

from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional

from coded_tools.tools.servicenow import gate
from coded_tools.tools.servicenow.base import ServiceNowTool
from coded_tools.tools.servicenow.context import SLY_COMMIT_TOKEN_KEY
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import GateError
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.reporting import MARKER_CHANGE_VERIFIED
from coded_tools.tools.servicenow.reporting import report
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.gate import NEW_RECORD
from coded_tools.tools.servicenow.transport import GatewayResult


class ServiceNowCreateRecord(ServiceNowTool):
    """Open a new record, behind the same approval gate as any other write."""

    OPERATION: str = "create"

    async def run(self, route: BoundRoute, args: Mapping[str, Any],
                  sly_data: Dict[str, Any], context: ToolContext,
                  profile: Profile) -> Dict[str, Any]:
        """
        :param route: The resolved create route.
        :param args: ``fields`` — name/value pairs for the new record.
        :param sly_data: Supplies the commit token.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: The outcome, including the asynchronous case.
        :raises GateError: when the creation was not approved.
        """
        fields: Dict[str, Any] = self.writable_fields(route.entity, args.get("fields"))

        token: Optional[str] = (sly_data or {}).get(SLY_COMMIT_TOKEN_KEY)
        verdict: gate.Verdict = gate.verify(profile, token, route.entity_name,
                                            NEW_RECORD, fields, sly_data)
        await report(args, context, {
            MARKER_CHANGE_VERIFIED: verdict.ok,
            "record": NEW_RECORD,
            "fields": sorted(fields),
            **verdict.as_dict(),
        }, content=("Approval verified." if verdict.ok
                    else f"Approval rejected: {verdict.reason}"))

        if not verdict.ok:
            raise GateError(
                f"This creation has not been approved: {verdict.reason}.",
                reason=verdict.result)

        body: Dict[str, Any] = dict(fields)
        if profile.correlation_field:
            body[profile.correlation_field] = context.correlation_id

        result: GatewayResult = await self.gateway(profile).call(
            route, args, context, body=body)

        sly_data.pop(SLY_COMMIT_TOKEN_KEY, None)

        if result.status == 202:
            return {
                "status": "accepted_pending",
                "http_status": 202,
                "detail": "The gateway accepted the request for asynchronous processing "
                          "and returned no record reference. Confirm by querying for the "
                          "correlation id below before considering another attempt — "
                          "resending blind would create a duplicate.",
                "correlation_id": context.correlation_id,
                "correlation_field": profile.correlation_field,
            }

        return {
            "status": "created",
            "http_status": result.status,
            "result": result.body,
        }
