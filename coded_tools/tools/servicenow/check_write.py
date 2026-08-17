"""
Write connectivity probe — debug a PUT (update) or POST (create) from the CLI,
the way check_connection.py debugs a GET.

This deliberately BYPASSES the human-approval gate. It is NOT how writes happen in
production — the agent network always goes propose -> approve -> commit. This tool
exists only to answer the transport question in isolation: does the write endpoint
stitch correctly, does the gateway accept the method + body + headers, and what
does it return? Prove that here first, then exercise the gated flow.

It uses the SAME profile, router, transport and field allow-list as the real write,
so a pass is evidence about the actual code path — only the gate is skipped.

Safety:
  * DRY RUN by default: it prints the exact request (method, URL, body) and sends
    NOTHING. Add --confirm to actually send it.
  * A write MUTATES a real record. Point it at a dev instance, prefer an
    append-only field (work_notes), and use a throwaway record.
  * Only fields on the entity's write_fields allow-list are accepted, exactly as
    the gated path enforces.

    PYTHONPATH=. python coded_tools/tools/servicenow/check_write.py \
        --entity request --record REQ0001 --set work_notes="probe test"
    # add --confirm to send it; SN_DEBUG=1 traces the stitched URL + PING/PONG.

Exit code 0 = the probe did what was asked (dry run built, or confirmed send got a
2xx); 1 = a failure, with a named remedy.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

# Standalone execution support: resolve the repo root when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
from coded_tools.tools.servicenow.base import ServiceNowTool  # noqa: E402
from coded_tools.tools.servicenow.check_connection import FAIL  # noqa: E402
from coded_tools.tools.servicenow.check_connection import PASS  # noqa: E402
from coded_tools.tools.servicenow.check_connection import check_credentials  # noqa: E402
from coded_tools.tools.servicenow.check_connection import check_profile  # noqa: E402
from coded_tools.tools.servicenow.check_connection import check_token  # noqa: E402
from coded_tools.tools.servicenow.check_connection import load_dotenv_if_present  # noqa: E402
from coded_tools.tools.servicenow.check_connection import say  # noqa: E402
from coded_tools.tools.servicenow.context import ToolContext  # noqa: E402
from coded_tools.tools.servicenow.errors import ServiceNowError  # noqa: E402
from coded_tools.tools.servicenow.profile import Profile  # noqa: E402
from coded_tools.tools.servicenow.router import Router  # noqa: E402
from coded_tools.tools.servicenow.router import Shape  # noqa: E402
from coded_tools.tools.servicenow.transport import Gateway  # noqa: E402


def parse_set(pairs: List[str]) -> Dict[str, str]:
    """
    :param pairs: ``field=value`` strings from repeated --set flags.
    :return: A field/value mapping.
    :raises ValueError: on a pair without '='.
    """
    fields: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects field=value, got: {pair!r}")
        name, _, value = pair.partition("=")
        fields[name.strip()] = value
    return fields


async def ground_record(profile: Profile, entity: str, record: str) -> Optional[str]:
    """
    Resolve a display reference (e.g. a number) to the identifier the write targets,
    with a real read — mirroring what the gated commit does before it writes.

    :param profile: The deployment profile.
    :param entity: Logical entity.
    :param record: The display-field value the operator supplied.
    :return: The identifier value, or None if the record was not found.
    """
    route = Router(profile).resolve("read", entity)
    names = route.params
    wanted = route.spec.query_params
    params: Dict[str, Any] = {}
    if "display" in wanted:
        params[names["display"]] = "true"
    if "query" in wanted:
        params[names["query"]] = f"{route.entity.display_field}={record}"
    context = ToolContext.build({"origin_str": "write-check"}, {}, "WriteCheck", "read", entity)
    result = await Gateway(profile).call(route, {"origin_str": "write-check"},
                                         context, params=params)
    body = result.body if isinstance(result.body, dict) else {}
    rows = body.get("result", [])
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return None
    return str(rows[0].get(route.entity.identifier_field, "")) or None


def build_body(profile: Profile, route: Any, record_id: Optional[str],
               fields: Dict[str, Any], context: ToolContext) -> Tuple[Any, Dict[str, Any]]:
    """
    Build the bound route and JSON body exactly as the commit path does.

    Targeting a record is driven by whether one was supplied, not by the operation
    name: a write with a record binds and addresses it (in the path or the body,
    per the template); a write without one is a body-only send (create-style).

    :param profile: The deployment profile.
    :param route: The resolved (unbound) write route.
    :param record_id: The grounded identifier, or None for a body-only write.
    :param fields: The validated, code-normalised write fields.
    :param context: The invocation context (for the correlation stamp).
    :return: (bound route, body dict).
    """
    body: Dict[str, Any] = dict(fields)
    bound = route
    if record_id is not None:
        bound = route.bind_record(record_id)
        if not route.record_in_path:
            # The path template decides whether the id rides in the URL or the body.
            body[route.entity.identifier_field] = record_id
    if profile.correlation_field:
        body[profile.correlation_field] = context.correlation_id
    return bound, body


async def check_write(profile: Profile, entity: str, operation: str, record: Optional[str],
                      set_pairs: List[str], confirm: bool, raw_id: bool) -> bool:
    """
    Build and (optionally) send one write, printing the exact request either way.

    :param profile: The deployment profile.
    :param entity: Logical entity.
    :param operation: "update" (PUT) or "create" (POST).
    :param record: Record reference for an update.
    :param set_pairs: ``field=value`` strings for the body.
    :param confirm: When False, dry run: print the request and send nothing.
    :param raw_id: Treat --record as the identifier itself, skipping the grounding read.
    :return: True on success (dry run built, or confirmed send returned 2xx).
    """
    try:
        route = Router(profile).resolve(operation, entity)
    except ServiceNowError as error:
        say(FAIL, "route", error.message)
        if error.reason == "operation_disabled":
            print("         This operation ships disabled. Set "
                  f"operations.{operation}.enabled = true in the profile once the "
                  "endpoint is confirmed.")
        return False

    # This probe only speaks writes. A query-shaped operation is a read — refuse it
    # here rather than send an empty body to a GET endpoint.
    if route.spec.shape is not Shape.BODY:
        say(FAIL, "operation", f"'{operation}' is a '{route.spec.shape.value}'-shape "
            "operation, not a write. Use check_connection.py for reads; this probe "
            "handles body-shape (PUT/POST) operations only.")
        return False

    try:
        fields = ServiceNowTool.writable_fields(route.entity, parse_set(set_pairs))
    except ValueError as error:
        say(FAIL, "fields", str(error))
        return False
    except ServiceNowError as error:
        say(FAIL, "fields", error.message)
        print(f"         Writable fields for '{entity}': {sorted(route.entity.write_fields)}.")
        return False

    # Targeting is driven by --record, not the operation name: given one, address it
    # (create-style writes simply omit it). An operation whose path addresses a record
    # cannot be built without one.
    record_id: Optional[str] = None
    if record:
        if raw_id:
            record_id = record
        else:
            record_id = await ground_record(profile, entity, record)
            if record_id is None:
                say(FAIL, "record", f"no record matched "
                    f"{route.entity.display_field}={record} (cannot target the write). "
                    "Use --raw-id to pass the identifier directly.")
                return False
            say(PASS, "record", f"{record} -> {route.entity.identifier_field}={record_id}")
    elif route.record_in_path:
        say(FAIL, "record", f"operation '{operation}' addresses a record in its path "
            "but no --record was given.")
        return False

    context = ToolContext.build({"origin_str": "write-check"}, {}, "WriteCheck",
                                operation, entity)
    bound, body = build_body(profile, route, record_id, fields, context)

    # Show the exact request, dry run or not — this is the whole point of the probe.
    say(PASS, "request", f"{operation}:{entity} -> {bound.method} {bound.url}")
    print(f"         body: {json.dumps(body, default=str)}")

    if not confirm:
        print("\n  DRY RUN — nothing was sent. Review the request above, then add "
              "--confirm to send it.")
        return True

    print("\n  --confirm set: sending the write (this MUTATES a real record) ...")
    try:
        result = await Gateway(profile).call(bound, {"origin_str": "write-check"},
                                             context, body=body)
    except ServiceNowError as error:
        say(FAIL, operation, error.message)
        status = error.details.get("status_code")
        if status == 403:
            print("         403: token is valid, so this is a policy/header/path issue "
                  "on the write endpoint. Confirm the write call's required headers and "
                  "that this product/path is subscribed.")
        elif status == 404:
            print("         404: the write path or table does not match the gateway. "
                  f"Compare the URL above with the endpoint contract for {operation}.")
        elif status == 405:
            print("         405: wrong method for this endpoint. Check "
                  f"operations.{operation}.method against the contract (PUT vs POST).")
        elif status == 500 or status is None:
            print("         500: the endpoint rejected the body or a header. Compare the "
                  "body above with the contract; the record id may need to be in the path "
                  "({record}) rather than the body, or vice versa.")
        return False

    say(PASS, operation, f"HTTP {result.status} in {result.latency_ms} ms")
    print(f"         response: {json.dumps(result.body, default=str)[:500]}")
    print(f"         correlation_id: {context.correlation_id}")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    """
    :param argv: CLI arguments; defaults to sys.argv[1:].
    :return: Process exit code (0 = success).
    """
    parser = argparse.ArgumentParser(
        description="ServiceNow WRITE probe — bypasses the approval gate; dry run by default.")
    parser.add_argument("--entity", default=None,
                        help="logical entity to write (default: first in the profile)")
    parser.add_argument("--operation", default="update",
                        help="the write operation name from the profile — 'update', "
                             "'create', or any body-shape operation you have defined "
                             "(e.g. a second update namespace); default update")
    parser.add_argument("--record", default=None,
                        help="record reference for an update (grounded to the identifier "
                             "unless --raw-id)")
    parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                        help="a field to write; repeatable. Only write_fields are allowed.")
    parser.add_argument("--raw-id", action="store_true",
                        help="treat --record as the identifier value directly (skip the "
                             "grounding read)")
    parser.add_argument("--confirm", action="store_true",
                        help="actually send the write; without it this is a dry run")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the package's own audit log lines")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.CRITICAL if args.quiet else logging.INFO,
                        format="%(message)s")

    print("ServiceNow WRITE probe (gate bypassed — connectivity only)")
    print("---------------------------------------------------------")

    from coded_tools.tools.servicenow import debuglog  # pylint: disable=import-outside-toplevel
    if debuglog.enable_debug_if_requested():
        print("  (SN_DEBUG on: full URL + param tracing to stderr)")

    loaded = load_dotenv_if_present()
    if loaded:
        print(f"  (.env loaded from {loaded}; values already in the shell win)")

    if not args.set:
        say(FAIL, "fields", "nothing to write — pass at least one --set field=value.")
        return 1

    profile = check_profile()
    if profile is None:
        return 1
    if not check_credentials(profile, profile.auth.style):
        return 1
    if not check_token(profile, profile.auth.style):
        return 1

    entity = args.entity or next(iter(profile.entities))
    ok = asyncio.run(check_write(profile, entity, args.operation, args.record,
                                 args.set, args.confirm, args.raw_id))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
