"""
Connection check — run this FIRST, before anything agent-shaped.

Proves the four things everything else depends on, in order, stopping at the
first failure with a named remedy:

  1. The deployment profile loads and validates.
  2. The credential environment variables are present (names checked, values
     never printed).
  3. A bearer token can be obtained with the configured auth style.
  4. One record can be read through the same router/transport the tools use.

No neuro-san server, no LLM key, no agent network involved — if this passes, a
failure later is in the agent layer; if this fails, nothing above it can work.
It exercises the package's real modules, not a parallel implementation, so a
pass here is evidence about the actual code path.

Configuration comes from the environment, and — for developer convenience —
from a repo-root `.env` file, read exactly the way the studio launcher reads
it: values already in the shell always win. So the same `.env` that serves
`python run.py` serves this checker; in a cluster there is no `.env` and the
variables arrive from the Secret.

    PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py
    PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py --try-both
    PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py --entity request --record REQ0001

--try-both answers the standing auth question empirically: it attempts the
token exchange with BOTH known flows and reports which the gateway accepts,
so `auth.style` can be set from evidence rather than argument.

Exit code 0 = all checks passed; 1 = a check failed (CI-friendly).
"""

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

# Standalone execution support: resolve the repo root when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
from coded_tools.tools.servicenow.auth import PreEncodedBasic  # noqa: E402
from coded_tools.tools.servicenow.auth import StandardClientCredentials  # noqa: E402
from coded_tools.tools.servicenow.auth import TokenProvider  # noqa: E402
from coded_tools.tools.servicenow.context import ToolContext  # noqa: E402
from coded_tools.tools.servicenow.errors import ServiceNowError  # noqa: E402
from coded_tools.tools.servicenow.profile import Profile  # noqa: E402
from coded_tools.tools.servicenow.profile import load_profile  # noqa: E402
from coded_tools.tools.servicenow.router import Router  # noqa: E402
from coded_tools.tools.servicenow.transport import Gateway  # noqa: E402

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[----]"


def load_dotenv_if_present(path: Optional[Path] = None) -> Optional[str]:
    """
    Load a repo-root `.env` with launcher semantics: shell values always win.

    Exists so the one `.env` a developer already maintains for `python run.py`
    also feeds this checker — without it, credentials placed in `.env` were
    visible to the server but invisible here, a first-day stumble for exactly
    the person this script exists to help.

    :param path: Override of the file location; defaults to the repo root `.env`.
    :return: The path loaded, or None when no `.env` exists.
    """
    candidate = path if path is not None else _REPO_ROOT / ".env"
    if not candidate.is_file():
        return None
    try:
        from dotenv import load_dotenv  # pylint: disable=import-outside-toplevel
        load_dotenv(candidate, override=False)
    except ImportError:
        # Minimal fallback so the checker works even outside the full studio
        # environment. Same rule: never override what the shell already set.
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    return str(candidate)


def say(status: str, step: str, detail: str = "") -> None:
    """
    :param status: PASS/FAIL/SKIP marker.
    :param step: Short step name.
    :param detail: One-line detail.
    """
    line = f"  {status} {step}"
    if detail:
        line += f" - {detail}"
    print(line)


def check_profile() -> Optional[Profile]:
    """
    :return: The validated profile, or None after printing the failure.
    """
    source = os.environ.get("SN_PROFILE_FILE", "<unset>")
    try:
        profile = load_profile()
    except ServiceNowError as error:
        say(FAIL, "profile", f"{error.message}")
        print(f"         SN_PROFILE_FILE = {source}")
        print("         Remedy: point SN_PROFILE_FILE at a filled copy of "
              "profile.example.json; the message above names the offending key.")
        return None
    say(PASS, "profile", f"loaded from {source}; "
        f"{len(profile.operations)} operations, {len(profile.entities)} entities")
    return profile


def check_credentials(profile: Profile, style: str) -> bool:
    """
    :param profile: The deployment profile.
    :param style: The auth style whose variables to check.
    :return: True when every required variable is set. Values never printed.
    """
    auth = profile.auth
    needed: List[str] = [auth.client_id_env, auth.client_secret_env]
    if style == "preencoded":
        needed.append(auth.preencoded_credential_env)
    missing = [name for name in needed if not os.environ.get(name)]
    if missing:
        say(FAIL, f"credentials[{style}]", f"unset: {', '.join(missing)}")
        print("         Remedy: export them (from the secret manager, never a file "
              "in the repo).")
        return False
    say(PASS, f"credentials[{style}]", f"all present ({', '.join(needed)})")
    return True


def make_provider(profile: Profile, style: str) -> TokenProvider:
    """
    :param profile: The deployment profile.
    :param style: Which flow to build, independent of the profile's setting.
    :return: The provider.
    """
    auth = dataclasses.replace(profile.auth, style=style)
    timeout = (profile.limits.connect_timeout_seconds, profile.limits.timeout_seconds)
    cls = StandardClientCredentials if style == "standard" else PreEncodedBasic
    return cls(auth, profile.verify_tls, timeout)


def check_token(profile: Profile, style: str) -> bool:
    """
    :param profile: The deployment profile.
    :param style: The flow to attempt.
    :return: True on a successful mint. The token value is never printed.
    """
    started = time.perf_counter()
    try:
        token = make_provider(profile, style).fetch_token()
    except ServiceNowError as error:
        say(FAIL, f"token[{style}]", error.message)
        if error.reason == "auth_rejected":
            print("         The endpoint answered but refused these credentials with "
                  "this flow. If the other flow works (--try-both), set auth.style "
                  "accordingly; otherwise the credentials themselves are the problem.")
        elif error.reason == "auth_unreachable":
            print("         The token endpoint did not answer. Check auth.token_url, "
                  "network egress and any proxy between this host and the gateway.")
        return False
    ttl = int(token.expires_at - time.time())
    say(PASS, f"token[{style}]",
        f"obtained in {int((time.perf_counter() - started) * 1000)} ms, "
        f"expires in ~{ttl}s (value not shown)")
    return True


async def check_read(profile: Profile, entity: str, record: Optional[str]) -> bool:
    """
    :param profile: The deployment profile.
    :param entity: Logical entity to read.
    :param record: Optional display-field value to look up; otherwise first page.
    :return: True when the read succeeds.
    """
    try:
        route = Router(profile).resolve("read", entity)
    except ServiceNowError as error:
        say(FAIL, "route", error.message)
        return False
    # Print the fully resolved target so the operator sees exactly what is pinged.
    say(PASS, "route", f"read:{entity} -> {route.method} {route.url}")

    names = route.params
    wanted = route.spec.query_params
    # Send only the params this operation declares — mirroring the read tool, so the
    # check exercises the same request the tool would make (not a different one).
    params: Dict[str, Any] = {}
    if "display" in wanted:
        params[names["display"]] = "true"
    if "fields" in wanted:
        params[names["fields"]] = ",".join(route.entity.read_fields)
    if "exclude_reference_link" in wanted:
        params[names["exclude_reference_link"]] = "true"
    if "limit" in wanted:
        params[names["limit"]] = 1
    if "query" in wanted and record:
        params[names["query"]] = f"{route.entity.display_field}={record}"
    print(f"         params: {params}")

    context = ToolContext.build({"origin_str": "connection-check"}, {}, "ConnectionCheck",
                                "read", entity)
    try:
        result = await Gateway(profile).call(route, {"origin_str": "connection-check"},
                                             context, params=params)
    except ServiceNowError as error:
        say(FAIL, "read", error.message)
        status = error.details.get("status_code")
        if status == 403:
            print("         403 is an authorization/policy rejection. The token is "
                  "valid (it was obtained above), so this is usually a required "
                  "business-call header or an unsubscribed path/product at the "
                  "gateway. Confirm the read call's required headers with the "
                  "endpoint owner.")
        elif status == 404:
            print("         404 usually means the operation path or table in the "
                  "profile does not match the gateway. Compare the URL above with "
                  "the endpoint contract.")
        elif status == 500 or status is None:
            print("         500 often means the request carried a parameter this "
                  "endpoint does not accept. Compare the params above with the "
                  "endpoint contract, and trim query_params for this operation to "
                  "only what it supports.")
        return False

    body = result.body if isinstance(result.body, dict) else {}
    rows = body.get("result", [])
    count = len(rows) if isinstance(rows, list) else (1 if rows else 0)
    fields = sorted(rows[0].keys()) if count and isinstance(rows, list) else []
    say(PASS, "read",
        f"HTTP {result.status} in {result.latency_ms} ms; {count} record(s); "
        f"fields: {', '.join(fields) if fields else '(none returned)'}")
    if record and count == 0:
        say(FAIL, "record", f"no record matched {route.entity.display_field}={record}")
        return False
    print(f"         correlation_id for this check: {context.correlation_id}")
    print("         (grep it in the server/pod log to confirm the audit leg.)")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    """
    :param argv: CLI arguments; defaults to sys.argv[1:].
    :return: Process exit code (0 = every check passed).
    """
    parser = argparse.ArgumentParser(description="ServiceNow gateway connection check")
    parser.add_argument("--entity", default=None,
                        help="logical entity to read (default: first in the profile)")
    parser.add_argument("--record", default=None,
                        help="optional record reference to look up by display field")
    parser.add_argument("--try-both", action="store_true",
                        help="attempt BOTH auth flows and report which the gateway accepts")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the package's own audit log lines")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.CRITICAL if args.quiet else logging.INFO,
                        format="%(message)s")

    print("ServiceNow connection check")
    print("---------------------------")

    loaded = load_dotenv_if_present()
    if loaded:
        print(f"  (.env loaded from {loaded}; values already in the shell win)")

    profile = check_profile()
    if profile is None:
        return 1

    styles: List[str] = ([profile.auth.style, "preencoded" if profile.auth.style == "standard"
                          else "standard"] if args.try_both else [profile.auth.style])

    token_ok = False
    working_styles: List[str] = []
    for style in styles:
        if not check_credentials(profile, style):
            if style == profile.auth.style:
                return 1
            say(SKIP, f"token[{style}]", "not attempted (credentials missing)")
            continue
        if check_token(profile, style):
            working_styles.append(style)
            if style == profile.auth.style:
                token_ok = True

    if args.try_both:
        if working_styles:
            print(f"\n  auth flows accepted by this gateway: {', '.join(working_styles)}")
            if profile.auth.style not in working_styles and working_styles:
                print(f"  -> set auth.style to '{working_styles[0]}' in the profile.")
        else:
            print("\n  neither flow was accepted — credentials or endpoint are wrong.")
    if not token_ok:
        return 1

    entity = args.entity or next(iter(profile.entities))
    read_ok = asyncio.run(check_read(profile, entity, args.record))
    if not read_ok:
        return 1

    print("\nAll checks passed. The connection works; anything failing above this "
          "layer is agent configuration, not connectivity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
