"""
Per-invocation context and the sly_data key vocabulary.

A CodedTool is handed no session id, so the correlation id is created at first
touch and parked on sly_data. The framework's own interface documentation sanctions
this: sly_data may be used as a bulletin board for values shared between the tools
of one request, and its lifetime is exactly the request.

For a gated write the id additionally travels *inside* the signed commit token, so
the propose turn and the commit turn — which are separate requests with separate
sly_data — still reconcile to one identifier. That, plus stamping the same id onto
the downstream record, is what lets an investigator join the agent-side trail to
the system-of-record side and survive the pod.
"""

import uuid
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional

# sly_data keys owned by this package. Namespaced so they cannot collide with
# another tool family sharing the same request.
SLY_CORRELATION_KEY: str = "sn_correlation_id"
SLY_COMMIT_TOKEN_KEY: str = "sn_commit_token"
SLY_PROPOSAL_KEY: str = "sn_pending_change"
SLY_CONSUMED_KEY: str = "sn_consumed_tokens"

# {entity name: {display number: stored identifier}}, populated by reads.
# Writes resolve their target against this rather than trusting an identifier the
# model hands back, so a fabricated identifier can never reach the gateway.
SLY_RECORD_MAP: str = "sn_record_ids"

# Optional caller identity, supplied by the client on the sly_data rail. Never
# read from args: args are journaled verbatim into the chat stream.
SLY_USER_KEY: str = "sn_user_id"

# Framework-injected argument keys. Listed so validation can ignore them and so
# nothing in this package mistakes them for LLM-supplied input.
FRAMEWORK_ARG_KEYS: frozenset = frozenset({
    "origin", "origin_str", "progress_reporter", "reservationist", "runtime",
})


def correlation_id(sly_data: Optional[Dict[str, Any]]) -> str:
    """
    Return this request's correlation id, creating it on first touch.

    :param sly_data: The request's sly_data dictionary; may be None.
    :return: A stable hex correlation id for the life of the request.
    """
    if sly_data is None:
        return uuid.uuid4().hex
    existing: Any = sly_data.get(SLY_CORRELATION_KEY)
    if isinstance(existing, str) and existing:
        return existing
    fresh: str = uuid.uuid4().hex
    sly_data[SLY_CORRELATION_KEY] = fresh
    return fresh


@dataclass(frozen=True)
class ToolContext:
    """Identity and provenance for a single tool invocation."""

    correlation_id: str
    origin: str
    tool: str
    operation: str
    entity: str
    on_behalf_of: Optional[str] = None

    @classmethod
    def build(cls, args: Mapping[str, Any], sly_data: Optional[Dict[str, Any]],
              tool: str, operation: str = "", entity: str = "") -> "ToolContext":
        """
        :param args: The tool arguments, including the framework-injected keys.
        :param sly_data: The request's sly_data dictionary.
        :param tool: The concrete tool class name.
        :param operation: Logical operation, when already known.
        :param entity: Logical entity, when already known.
        :return: A populated ToolContext.
        """
        caller: Any = (sly_data or {}).get(SLY_USER_KEY)
        return cls(
            correlation_id=correlation_id(sly_data),
            origin=str(args.get("origin_str") or tool),
            tool=tool,
            operation=operation or str(args.get("operation") or ""),
            entity=entity or str(args.get("entity") or ""),
            on_behalf_of=str(caller) if caller else None,
        )

    def as_dict(self) -> Dict[str, Any]:
        """
        :return: The context as flat, JSON-serializable fields for a log or
                 progress structure. Contains no secret and no record content.
        """
        payload: Dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "origin": self.origin,
            "tool": self.tool,
        }
        if self.operation:
            payload["operation"] = self.operation
        if self.entity:
            payload["entity"] = self.entity
        if self.on_behalf_of:
            payload["on_behalf_of"] = self.on_behalf_of
        return payload
