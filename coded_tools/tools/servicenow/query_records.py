"""
Read records from a bound entity.

The entity is pinned in the agent's HOCON ``args``, so the model chooses *which
agent* to call, never *which table* to reach. That is what makes the allow-list a
control rather than a suggestion.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Mapping

from coded_tools.tools.servicenow import labels
from coded_tools.tools.servicenow.base import ServiceNowTool
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.transport import GatewayResult


class ServiceNowQueryRecords(ServiceNowTool):
    """Query records, with fields narrowed and pages bounded."""

    OPERATION: str = "read"

    async def run(self, route: BoundRoute, args: Mapping[str, Any],
                  sly_data: Dict[str, Any], context: ToolContext,
                  profile: Profile) -> Dict[str, Any]:
        """
        :param route: The resolved read route.
        :param args: ``query`` (optional encoded query), ``fields`` (optional subset),
                     ``limit`` and ``offset`` (optional paging).
        :param sly_data: The request's private channel (unused for reads).
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: Records plus explicit paging information.
        """
        names: Mapping[str, str] = route.params
        wanted = route.spec.query_params            # the params THIS operation sends
        fields: List[str] = self.readable_fields(route.entity, args.get("fields"))
        size: int = self.page_size(route, args.get("limit"))
        try:
            offset: int = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0

        # Build the query from only the params this operation declares it supports.
        # A gateway that accepts a limited set errors on anything beyond it, so the
        # profile — not the code — decides what is sent.
        server_limits: bool = "limit" in wanted
        params: Dict[str, Any] = {}
        if "display" in wanted:
            params[names["display"]] = "true"
        if "query" in wanted and args.get("query"):
            params[names["query"]] = self.normalize_query(args["query"])
        if "fields" in wanted:
            params[names["fields"]] = ",".join(fields)
        if "exclude_reference_link" in wanted:
            params[names["exclude_reference_link"]] = "true"
        if server_limits:
            # One more than asked for, so "is there another page" is answered by the
            # gateway rather than guessed from a full page.
            params[names["limit"]] = size + 1
            if "offset" in wanted:
                params[names["offset"]] = offset

        result: GatewayResult = await self.gateway(profile).call(
            route, args, context, params=params)

        records: List[Dict[str, Any]] = self.records_from(result.body)

        # Pagination is only meaningful when the gateway honoured a server-side
        # limit. Otherwise report the full result set with no "next page".
        if server_limits:
            has_more: bool = len(records) > size
            records = records[:size]
        else:
            has_more = False

        # Remember identifiers before translating anything, so writes can resolve
        # their target from the private channel instead of trusting the model.
        self.remember_records(route.entity_name, route.entity, records, sly_data)

        # Enforce the readable allow-list. When the gateway takes sysparm_fields the
        # server already narrowed; when it does not, narrow here so the same
        # deny-by-default guarantee holds either way.
        if "fields" not in wanted:
            allowed = set(fields)
            records = [{k: v for k, v in record.items() if k in allowed}
                       for record in records]

        # Translate coded values into labels. A code that arrives without its
        # meaning is a code a language model will invent a meaning for.
        rendered: List[Dict[str, Any]] = [labels.decode_record(route.entity, record)
                                          for record in records]

        return {
            "records": rendered,
            "count": len(rendered),
            "has_more": has_more,
            "next_offset": (offset + size) if has_more else None,
        }
