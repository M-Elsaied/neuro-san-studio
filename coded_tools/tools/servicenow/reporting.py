"""
Reporting on the framework's own rails.

This package introduces no event bus and no bespoke audit API. neuro-san already
provides both destinations:

  * **Live / investigable** — ``AgentProgressReporter``, handed to every CodedTool
    as ``args["progress_reporter"]`` and documented "to be used from within
    CodedTool.async_invoke()". It writes an AgentProgressMessage into the same
    stream the client and the trace already consume.
  * **Durable** — the standard library logger, named by the agent's origin path to
    match the framework's own logger-per-agent convention, so our lines interleave
    with the framework's ``tool_start``/``tool_end`` entries under one correlation
    id and land wherever the cluster ships stdout.

What the framework already emits — tool boundaries, the full argument dictionary,
outcome and error flag — is deliberately not repeated here.

Security note: progress structures are **not** redacted on the way to the client
(unlike sly_data, which the framework redacts in traces by default). The same rule
that governs args governs this module: field names always, field values only when
explicitly allow-listed, never a credential.
"""

import logging
import urllib.parse
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple

from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.scrub import scrub_mapping

# The five markers this package raises. Each exists only because the framework
# cannot see the thing it describes.
MARKER_POLICY_DENIED: str = "servicenow_policy_denied"
MARKER_CHANGE_PROPOSED: str = "servicenow_change_proposed"
MARKER_CHANGE_VERIFIED: str = "servicenow_change_verified"
MARKER_DOWNSTREAM_CALL: str = "servicenow_downstream_call"
MARKER_AUTH: str = "servicenow_auth"

#: Identifier keys this package mints itself and whose values must survive
#: scrubbing intact. The shape patterns cannot tell a secret from one of our own
#: identifiers — a correlation id and a payload hash are long opaque
#: alphanumerics, exactly the shape of a leaked token — and without this list the
#: scrubber redacted the very join key the audit trail exists to carry, in every
#: event, silently. Nothing else is exempt: values under any other key still scrub.
AUDIT_SAFE_KEYS: frozenset = frozenset({"correlation_id", "payload_hash", "token_id"})


def _logger(args: Mapping[str, Any]) -> logging.Logger:
    """
    :param args: The tool arguments, carrying the framework-injected origin path.
    :return: A logger named the way the framework names its own.
    """
    return logging.getLogger(str(args.get("origin_str") or __name__))


def build_event(context: ToolContext, structure: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Merge context and marker payload into one scrubbed, JSON-safe event.

    :param context: The invocation context.
    :param structure: The marker payload.
    :return: The event dictionary.
    """
    event: Dict[str, Any] = context.as_dict()
    event.update(structure)
    return scrub_mapping(event, safe_keys=AUDIT_SAFE_KEYS)


def _flatten(value: Any, prefix: str, into: List[Tuple[str, str]]) -> None:
    """
    Flatten a nested event into dotted key/value string pairs.

    :param value: The node to flatten.
    :param prefix: The dotted key so far.
    :param into: Accumulator of (key, value-as-string) pairs.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            _flatten(item, f"{prefix}.{key}" if prefix else str(key), into)
    elif isinstance(value, (list, tuple)):
        into.append((prefix, ",".join(str(item) for item in value)))
    elif isinstance(value, bool):
        into.append((prefix, "true" if value else "false"))
    elif value is None:
        into.append((prefix, ""))
    else:
        into.append((prefix, str(value)))


def audit_line(event: Mapping[str, Any]) -> str:
    """
    Render one event as an envelope-safe, machine-parseable audit line.

    Why not a JSON line: measured against the deployed server, the framework's log
    formatter wraps whatever a logger emits inside its own structured envelope
    *without escaping it* — a JSON message containing quotes makes the composite
    line invalid JSON, so a SIEM's JSON parser rejected every audit line we wrote.
    The envelope is the framework's; its formatter is not this package's to fix.

    So the line makes itself safe under any naive wrapper: sorted logfmt pairs with
    every value percent-encoded. The output contains no quote, backslash, brace or
    newline — nothing that can break out of a surrounding string — while staying
    both greppable and mechanically decodable (split on spaces and '=', unquote the
    value). Splunk-style key=value extraction reads it natively.

    The envelope also contributes fields of its own — Timestamp, request_id,
    user_id — for free on every line. request_id is the framework's native
    per-request correlation and joins our lines to its own journal entries; our
    correlation_id is the one that additionally spans the propose and commit turns
    and is stamped on the downstream record.

    :param event: The (already scrubbed) event dictionary.
    :return: One space-separated line of key=value pairs, keys sorted.
    """
    pairs: List[Tuple[str, str]] = []
    _flatten(event, "", pairs)
    rendered: List[str] = [
        f"{key}={urllib.parse.quote(value, safe='-._~')}"
        for key, value in sorted(pairs)
    ]
    return " ".join(rendered)


async def report(args: Mapping[str, Any], context: ToolContext,
                 structure: Mapping[str, Any], content: str = "") -> Dict[str, Any]:
    """
    Fan one event out to both framework destinations.

    :param args: The tool arguments (supplies the progress reporter and origin).
    :param context: The invocation context.
    :param structure: The marker payload; must be JSON-serializable.
    :param content: Optional human-readable note for the client.
    :return: The event that was emitted, for assertion in tests.
    """
    event: Dict[str, Any] = build_event(context, structure)
    _logger(args).info(audit_line(event))

    reporter: Optional[Any] = args.get("progress_reporter")
    if reporter is not None:
        try:
            await reporter.async_report_progress(structure=event, content=content)
        except Exception as exception:  # pylint: disable=broad-exception-caught
            # Reporting must never be the reason a tool fails. The durable log line
            # above has already been written, so the trail survives regardless.
            _logger(args).warning("progress reporting failed: %s", type(exception).__name__)
    return event
