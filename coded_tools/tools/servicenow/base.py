"""
The single invocation pipeline every ServiceNow tool shares.

The three copy-pasted tools this replaces had already drifted apart on their error
contract before they shipped, which is the argument for putting the pipeline in one
place: validation, policy, the gate, transport and reporting are decided once, and a
new tool inherits all of it by declaring only what makes it different.

Two framework facts shape the contract here:

  * A return value is **stringified, not JSON-serialised**, by the activation layer.
    So every path — success and error alike — returns ``json.dumps(...)``, or the
    model receives a Python repr with single quotes.
  * Arguments are **journaled verbatim** into the chat stream. Nothing sensitive may
    travel in ``args``; the approval token therefore lives only on sly_data.
"""

import json
import re
import urllib.parse
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Tuple

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.tools.servicenow import labels
from coded_tools.tools.servicenow.context import SLY_PROPOSAL_KEY
from coded_tools.tools.servicenow.context import SLY_RECORD_MAP
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import PolicyDenied
from coded_tools.tools.servicenow.errors import ServiceNowError
from coded_tools.tools.servicenow.profile import EntityConfig
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.profile import get_profile
from coded_tools.tools.servicenow.reporting import MARKER_POLICY_DENIED
from coded_tools.tools.servicenow.reporting import report
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.router import get_router
from coded_tools.tools.servicenow.transport import Gateway

#: The shapes a legitimate record number or identifier can take. Anything else is
#: refused before it can be embedded in a lookup query.
_SAFE_REFERENCE: re.Pattern = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@._\- ]{0,63}")


class ServiceNowTool(CodedTool):
    """
    Base for every tool in this family.

    Subclasses override :meth:`run` and, usually, nothing else.
    """

    #: Fallback when the agent's pinned args omit an operation.
    OPERATION: str = "read"

    def __init__(self, gateway: Optional[Gateway] = None):
        """
        :param gateway: Injected gateway, used by tests. Production instantiation is
                        argument-free, as the framework's contract requires.
        """
        self._gateway: Optional[Gateway] = gateway

    def gateway(self, profile: Profile) -> Gateway:
        """
        :param profile: The deployment profile.
        :return: The gateway to issue calls through.
        """
        return self._gateway or Gateway(profile)

    # ---------------------------------------------------------------- pipeline

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Run the shared pipeline: resolve, authorise, act, report.

        :param args: Tool arguments. ``operation`` and ``entity`` are pinned by the
                     agent's HOCON ``args`` block, never chosen by the model.
        :param sly_data: The request's private channel.
        :return: A JSON string. Always JSON, including on failure.
        """
        tool_name: str = type(self).__name__
        operation: str = str(args.get("operation") or self.OPERATION)
        entity: str = str(args.get("entity") or "")
        context: ToolContext = ToolContext.build(args, sly_data, tool_name, operation, entity)

        try:
            profile: Profile = get_profile()
            route: BoundRoute = get_router().resolve(operation, entity)
            result: Dict[str, Any] = await self.run(route, args, sly_data, context, profile)
            result.setdefault("correlation_id", context.correlation_id)
            return json.dumps(result, default=str)

        except PolicyDenied as denied:
            await report(args, context,
                         {MARKER_POLICY_DENIED: True,
                          "reason": denied.reason,
                          "detail": denied.message,
                          **denied.details},
                         content=f"Refused: {denied.message}")
            return self._failure(denied, context)

        except ServiceNowError as error:
            return self._failure(error, context)

    async def run(self, route: BoundRoute, args: Mapping[str, Any],
                  sly_data: Dict[str, Any], context: ToolContext,
                  profile: Profile) -> Dict[str, Any]:
        """
        Do the tool's actual work.

        :param route: The resolved route.
        :param args: Tool arguments.
        :param sly_data: The request's private channel.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: A JSON-serializable result dictionary.
        """
        raise NotImplementedError

    @staticmethod
    def _failure(error: ServiceNowError, context: ToolContext) -> str:
        """
        :param error: The error to render.
        :param context: The invocation context.
        :return: A JSON error payload carrying the correlation id, so a human can
                 find the whole chain in the logs from whatever the model reports.
        """
        payload: Dict[str, Any] = error.as_dict()
        payload["correlation_id"] = context.correlation_id
        return json.dumps(payload, default=str)

    # ------------------------------------------------------------- validation

    @staticmethod
    def require(args: Mapping[str, Any], name: str) -> Any:
        """
        :param args: Tool arguments.
        :param name: The required argument name.
        :return: The value.
        :raises PolicyDenied: when absent, so the refusal is auditable like any other.
        """
        value: Any = args.get(name)
        if value in (None, "", [], {}):
            raise PolicyDenied(f"Missing required argument '{name}'.",
                               reason="missing_argument", argument=name)
        return value

    @staticmethod
    def readable_fields(entity: EntityConfig, requested: Optional[Any]) -> List[str]:
        """
        Narrow a field request to the entity's readable allow-list.

        An unknown field is refused rather than silently dropped: silently dropping
        it would let an agent believe it had asked for something it never received.

        :param entity: The entity configuration.
        :param requested: Comma-separated string or sequence, or None for all allowed.
        :return: The fields to request, always including the identifier.
        :raises PolicyDenied: if a requested field is not readable.
        """
        allowed: Tuple[str, ...] = entity.read_fields
        if not requested:
            chosen: List[str] = list(allowed)
        else:
            names: Iterable[str] = (requested.split(",") if isinstance(requested, str)
                                    else requested)
            chosen = [str(name).strip() for name in names if str(name).strip()]
            unknown: List[str] = sorted({name for name in chosen if name not in allowed})
            if unknown:
                raise PolicyDenied(
                    f"Fields not readable for this entity: {unknown}.",
                    reason="field_not_readable", fields=unknown)
        if entity.identifier_field not in chosen:
            # Always retained: without it a follow-up change could not target the record.
            chosen.insert(0, entity.identifier_field)
        return chosen

    @staticmethod
    def writable_fields(entity: EntityConfig, fields: Any) -> Dict[str, Any]:
        """
        Validate a proposed write against the entity's writable allow-list, and
        normalise any human labels back to the codes the system stores.

        Normalisation happens here, before the approval payload is hashed, so a
        proposal and its commit normalise identically and the signature still
        matches whichever form the model used each time.

        :param entity: The entity configuration.
        :param fields: A mapping of field name to new value.
        :return: The validated, code-normalised mapping.
        :raises PolicyDenied: if the shape is wrong or any field is not writable.
        """
        if not isinstance(fields, Mapping) or not fields:
            raise PolicyDenied(
                "Argument 'fields' must be a non-empty object of field/value pairs.",
                reason="missing_argument", argument="fields")
        refused: List[str] = sorted({str(name) for name in fields
                                     if str(name) not in entity.write_fields})
        if refused:
            raise PolicyDenied(
                f"Fields not writable for this entity: {refused}. "
                f"Writable fields are: {sorted(entity.write_fields)}.",
                reason="field_not_writable", fields=refused)

        supplied: Dict[str, Any] = {str(name): value for name, value in fields.items()}
        encoded: Dict[str, Any] = labels.encode_fields(entity, supplied)

        # A coded field whose value matched neither a code nor a label is a guess.
        # Refusing beats writing an uninterpretable value into a live record.
        for name, value in encoded.items():
            allowed: Optional[str] = labels.describe(entity, name)
            if allowed is None:
                continue
            if str(value) not in entity.coded_fields.get(name, {}):
                raise PolicyDenied(
                    f"'{value}' is not a recognised value for '{name}'. "
                    f"Permitted values are: {allowed}.",
                    reason="unrecognised_value", field=name)
        return encoded

    # ------------------------------------------------------------- identifiers

    @staticmethod
    def remember_records(entity_name: str, entity: EntityConfig,
                         records: List[Dict[str, Any]],
                         sly_data: Optional[Dict[str, Any]]) -> None:
        """
        Record the identifiers a read returned, on the private channel.

        This is what later lets a write resolve its target without trusting an
        identifier echoed back by the model.

        :param entity_name: Logical entity name.
        :param entity: The entity configuration.
        :param records: Records as returned by the gateway.
        :param sly_data: The request's private channel.
        """
        if sly_data is None:
            return
        known: Dict[str, Dict[str, str]] = dict(sly_data.get(SLY_RECORD_MAP) or {})
        for_entity: Dict[str, str] = dict(known.get(entity_name) or {})
        for record in records:
            identifier: Any = record.get(entity.identifier_field)
            display: Any = record.get(entity.display_field)
            if identifier:
                for_entity[str(identifier)] = str(identifier)
                if display:
                    for_entity[str(display)] = str(identifier)
        known[entity_name] = for_entity
        sly_data[SLY_RECORD_MAP] = known

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def resolve_record(self, entity_name: str, entity: EntityConfig, supplied: str,
                             args: Mapping[str, Any], sly_data: Optional[Dict[str, Any]],
                             context: ToolContext, profile: Profile,
                             allow_lookup: bool = True) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Turn whatever the model supplied into a grounded identifier.

        The rule: an identifier reaching a write endpoint must have been produced by
        the gateway — either read earlier in this request, or confirmed to exist by a
        lookup now. It is never taken verbatim from the model, so a fabricated
        identifier fails here, by name, instead of targeting something real.

        Lookup tries the human-meaningful display value first, because that is the
        form a person can verify in a diff, then the identifier itself.

        Note what this does *not* claim: it stops a *fabricated* reference, not a
        *wrong but real* one. The human approving the diff remains the check on
        "right record", which is why the diff shows before-and-after values.

        :param entity_name: Logical entity name.
        :param entity: The entity configuration.
        :param supplied: What the caller passed as the record reference.
        :param args: Tool arguments, for the reporting rails.
        :param sly_data: The request's private channel.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :param allow_lookup: When False, resolve only from the private channel and
                             otherwise return the reference untouched, leaving the
                             approval gate to arbitrate. Used by commit, where a
                             legitimate call was always preceded by a proposal that
                             already recorded the identifier — so a miss means "not
                             approved", and saying that is more use than a lookup.
        :return: (identifier, record if one was fetched during resolution)
        :raises ServiceNowError: when the reference cannot be grounded.
        """
        reference: str = str(supplied)
        known: Mapping[str, str] = (sly_data or {}).get(SLY_RECORD_MAP, {}).get(entity_name, {})
        if reference in known:
            return known[reference], None

        if not allow_lookup:
            # The pending proposal also grounds a reference: it recorded both the
            # resolved identifier and the reference the caller used, so a commit
            # still resolves even when the client returned the token but not the
            # record map. The gate remains the arbiter either way.
            pending: Mapping[str, Any] = (sly_data or {}).get(SLY_PROPOSAL_KEY) or {}
            if (pending.get("entity") == entity_name
                    and reference in (pending.get("reference"), pending.get("record"))):
                return str(pending["record"]), None
            return reference, None

        if not _SAFE_REFERENCE.fullmatch(reference):
            # The reference is about to be embedded in an encoded query. Refusing
            # odd characters here closes query injection through the grounding
            # lookup — the blast radius would only be reads this caller could make
            # anyway, but an input that reaches a wire query unchecked is wrong on
            # principle.
            raise ServiceNowError(
                "The record reference contains characters that are never part of "
                "a record number or identifier, so it was not looked up.",
                reason="record_not_grounded", record=reference[:64])

        record: Optional[Dict[str, Any]] = None
        for field in (entity.display_field, entity.identifier_field):
            record = await self.fetch_by(entity_name, field, reference,
                                         args, context, profile)
            if record is not None:
                break

        if record is None:
            raise ServiceNowError(
                f"No record matching '{reference}' exists, so there is nothing to change. "
                f"Read the record first and refer to it by the value shown.",
                reason="record_not_grounded", record=reference)

        identifier: Any = record.get(entity.identifier_field)
        if not identifier:
            raise ServiceNowError(
                f"The record found for '{reference}' carries no "
                f"'{entity.identifier_field}', so it cannot be targeted safely.",
                reason="record_not_grounded", record=reference)
        self.remember_records(entity_name, entity, [record], sly_data)
        return str(identifier), record

    # ----------------------------------------------------------------- lookup

    async def fetch_by(self, entity_name: str, field: str, value: str,
                       args: Mapping[str, Any], context: ToolContext,
                       profile: Profile) -> Optional[Dict[str, Any]]:
        """
        Fetch a single record by any field, through the read route.

        :param entity_name: Logical entity name.
        :param field: The field to match on.
        :param value: The value to match.
        :param args: Tool arguments, for the reporting rails.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: The record as stored (codes not translated), or None.
        """
        route: BoundRoute = get_router().resolve("read", entity_name)
        names: Mapping[str, str] = route.params
        params: Dict[str, Any] = {
            names["display"]: "true",
            names["exclude_reference_link"]: "true",
            names["fields"]: ",".join(route.entity.read_fields),
            names["limit"]: 1,
            names["query"]: f"{field}={value}",
        }
        result = await self.gateway(profile).call(route, args, context, params=params)
        records: List[Dict[str, Any]] = self.records_from(result.body)
        return records[0] if records else None

    async def fetch_one(self, entity_name: str, record_id: str, args: Mapping[str, Any],
                        context: ToolContext, profile: Profile) -> Optional[Dict[str, Any]]:
        """
        Fetch a single record by its identifier, for diffing or confirmation.

        :param entity_name: Logical entity name.
        :param record_id: The record identifier.
        :param args: Tool arguments, for the reporting rails.
        :param context: The invocation context.
        :param profile: The deployment profile.
        :return: The record, or None when it does not exist.
        """
        entity: EntityConfig = get_router().resolve("read", entity_name).entity
        return await self.fetch_by(entity_name, entity.identifier_field, record_id,
                                   args, context, profile)

    # ------------------------------------------------------------------ pages

    @staticmethod
    def page_size(route: BoundRoute, requested: Any) -> int:
        """
        Resolve a page size, defaulted in code and capped by the profile.

        The default lives here rather than in a schema description because a
        documented-but-unimplemented default is how an under-specified call ends up
        pulling an entire table into the context window.

        :param route: The bound route.
        :param requested: The caller's requested size, or None.
        :return: A safe page size.
        """
        limits = route.limits
        try:
            size: int = int(requested) if requested is not None else limits.default_page
        except (TypeError, ValueError):
            size = limits.default_page
        return max(1, min(size, limits.max_page))

    @staticmethod
    def normalize_query(query: Any) -> str:
        """
        Undo a pre-encoded query so it is not encoded twice.

        A model told it is passing an "encoded query" will sometimes helpfully
        percent-encode it first. The HTTP layer then encodes the ``%`` again and the
        gateway matches nothing — returning an empty result set rather than an
        error, which is the hardest kind of failure to notice.

        :param query: The caller's query string.
        :return: A query safe to hand to the HTTP layer for encoding.
        """
        text: str = str(query).strip()
        if "%3D" in text.upper() or "%5E" in text.upper():
            return urllib.parse.unquote(text)
        return text

    @staticmethod
    def records_from(body: Any) -> List[Dict[str, Any]]:
        """
        :param body: A parsed response body.
        :return: The record list, tolerating both the wrapped and bare shapes.
        """
        if isinstance(body, Mapping):
            result: Any = body.get("result", [])
            if isinstance(result, Mapping):
                return [dict(result)]
            if isinstance(result, Sequence):
                return [dict(item) for item in result if isinstance(item, Mapping)]
            return []
        if isinstance(body, Sequence):
            return [dict(item) for item in body if isinstance(item, Mapping)]
        return []
