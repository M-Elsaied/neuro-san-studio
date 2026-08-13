# ServiceNow tool family for neuro-san

Agents **read** records in a ServiceNow-style system through an API gateway, and
**change** them only after a person approves the exact change. All endpoints,
tables and credentials come from a config file at runtime — the code contains
none, and a test fails the build if one appears.

- Review registers travel as tests: `test_review_findings.py` (R1–R5), `test_fundamentals.py` (F1–F3)
- 158 tests, ~10 s, no credentials, no network beyond loopback
- Requires `neuro-san>=0.6` (this repo's pin qualifies)

---

## 1. Folder structure — where everything lives

```
neuro-san-studio/
│
├── coded_tools/tools/servicenow/          THE PACKAGE (all Python lives here)
│   ├── check_connection.py                << START HERE - connection test
│   ├── README.md                          this file
│   ├── profile.example.json               config template (placeholders only)
│   │
│   ├── base.py                            shared pipeline every tool runs
│   ├── query_records.py                   read tool
│   ├── propose_change.py                  write tool, phase 1 (diff + approval)
│   ├── commit_change.py                   write tool, phase 2 (apply if approved)
│   ├── create_record.py                   create tool (ships disabled)
│   ├── profile.py / router.py             config loading + endpoint routing
│   ├── auth.py / transport.py             tokens + the only HTTP in the package
│   ├── gate.py / labels.py / scrub.py     approval tokens, code<->label, redaction
│   ├── context.py / reporting.py / errors.py
│   │
│   └── demo/                              local FAKE gateway, for trying things
│       ├── run_stub_gateway.py            run this to get a gateway on :8099
│       └── stub_gateway.py                (writes profile.local.json, gitignored)
│
├── registries/tools/servicenow.hocon      the agent network (logic only, no URLs)
├── registries/tools/manifest.hocon        the on/off switch:
│                                            "tools/servicenow.hocon": false
│
└── tests/coded_tools/tools/servicenow/    the full test suite (158 tests)
```

One rule explains the layout: **shapes are code, destinations are data.** Python
never contains an endpoint; the registry never contains a deployment value; the
profile file carries everything environment-specific.

---

## 2. First run — prove the connection (2 minutes)

Before any agent, server or LLM key: run the connection check. It proves
**profile → credentials → token → one read**, stopping at the first failure with
a named remedy. Exit code 0 = the approach works; anything failing later is
agent configuration, not connectivity.

**Where configuration comes from:** the environment. For local convenience the
checker (and the studio launcher) also read a repo-root **`.env`** — shell
values always win, and in a cluster there is no `.env`: the same variables
arrive from a Kubernetes Secret. `.env.example` documents every `SN_*` variable
with demo-ready values; copy its ServiceNow block into your `.env` once and
every local flow below just works.

### Option A — no gateway yet? Try it against the bundled fake one

Step 1 — add the ServiceNow block from `.env.example` to your `.env`
(uncommented; the demo values are fine as-is).

Step 2 — terminal 1: start the fake ServiceNow (port 8099; it also writes the
matching profile file your `.env` points at):

```bash
PYTHONPATH=. python coded_tools/tools/servicenow/demo/run_stub_gateway.py
```

Step 3 — terminal 2: run the check. Nothing to export — it reads your `.env`:

```bash
PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py
```

Expected:

```
  [PASS] profile - loaded from ...; 2 operations, 1 entities
  [PASS] credentials[standard] - all present (...)
  [PASS] token[standard] - obtained in 8 ms, expires in ~3600s (value not shown)
  [PASS] route - read:request -> GET (URL from profile)
  [PASS] read - HTTP 200 in 11 ms; 1 record(s); fields: number, short_description, ...
All checks passed.
```

### Option B — against a real gateway

1. Copy `profile.example.json`, fill in the real base URL, token URL, paths and
   table names, and save it somewhere OUTSIDE the repo. **Every `REPLACE_…` token
   must be replaced** — the profile is validated at load and a leftover placeholder
   (or a missing required key) fails fast, naming the offending key.

   A minimal, read-only profile looks like this once filled in (values below are
   illustrative `example.com` stand-ins — swap in your real gateway's; keep this
   file gitignored). Anything not shown here keeps the template's defaults:

   ```json
   {
     "base_url": "https://gateway.example.com/api/1.0/snow",
     "auth": {
       "style": "standard",
       "token_url": "https://gateway.example.com/oauth/token",
       "client_id_env": "SN_CLIENT_ID",
       "client_secret_env": "SN_CLIENT_SECRET"
     },
     "operations": {
       "read": {
         "method": "GET",
         "path": "/readnamespace/{table}",
         "shape": "query",
         "query_params": ["display", "query"]
       }
     },
     "entities": {
       "incident": {
         "table": "incident",
         "identifier_field": "sys_id",
         "display_field": "number",
         "read_fields": ["sys_id", "number", "short_description", "state"],
         "write_fields": [],
         "coded_fields": { "state": { "1": "New", "2": "In Progress" } }
       }
     },
     "gate": { "keys_env": "SN_GATE_KEYS" }
   }
   ```

   The full `base_url` matters: it must include every fixed path segment the
   gateway expects **before** the operation `path` (e.g. an API version and product
   prefix). `base_url` + `operations.read.path` (with `{table}` substituted) is the
   URL that gets hit — turn on `SN_DEBUG=1` (see §5) to see it stitched. Add
   `update`/`create` operations and `write_fields` only when you're doing writes.
2. Point the same `SN_*` variables at the real thing — in your `.env` locally,
   or as shell exports (shell wins over `.env`):

```bash
export SN_PROFILE_FILE=/path/to/your/profile.json
export SN_CLIENT_ID=...      SN_CLIENT_SECRET=...     # from the secret manager
PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py --try-both
```

`--try-both` attempts BOTH known token flows and prints which one the gateway
accepts — set `auth.style` in the profile from that answer. Other useful flags:
`--record REQ0001` to look up one specific record, `--quiet` to hide the
audit log lines.

Reading a failure: the checker names the missing env var, the broken profile
key, or the refused step — and a 404 on the read step means the *path or table
in the profile doesn't match the gateway* (an endpoint-contract problem to
report, not code to debug).

**Still stuck on which URL was actually hit?** Re-run with `SN_DEBUG=1` in front
of the command — it prints the token exchange, the stitched `base_url + path`
breakdown, and the exact wire URL that was called (see **§5 → Debugging URL
stitching** for the full trace and its confidentiality caveat). This is the
fastest way to catch a `base_url` that's missing a path segment.

**Token mints but the read gets a 500 / "Technical error occurred in API
Gateway"?** The URL is reaching the gateway but the request is missing something
it demands. The fastest way to find it: open the same call in a tool that works
(e.g. Postman), export it as **cURL**, and diff the headers against ours (the
`SN_DEBUG` `PING` line lists our header *names*). A gateway commonly requires an
extra per-call header — an API **key id** — beyond the bearer token; add it with
`auth.extra_headers` (`{ "keyId": "env:SN_KEY_ID" }`) and set `SN_KEY_ID`, no
code change. If instead the body is an HTML challenge page, a WAF is blocking the
client rather than the gateway rejecting the request.

---

## 3. What do you want to do?

Almost everything is a config change. If your task is in this table, do exactly
what it says and touch nothing else.

| I want to… | Change this | Python? |
|---|---|---|
| **Prove the connection works** | §2 above — `check_connection.py` | no |
| Run the tests | `pytest -o addopts= tests/coded_tools/tools/servicenow -q` | no |
| **Point at a real gateway** | Copy `profile.example.json`, fill it, set `SN_PROFILE_FILE` | **no** |
| **Add a new endpoint** (of a known shape) | Add an `operations.<name>` entry — see §3a worksheet | no |
| Change an endpoint path / HTTP method | `operations.<name>.path` / `.method` in the profile | no |
| Change which query params an endpoint sends | `operations.<name>.query_params` — send only what the gateway accepts | no |
| Put one operation on a different host or API version | `operations.<name>.base_url` in the profile | no |
| Add a table the agents may touch | Add an `entities.<name>` block in the profile **+** one agent block in `registries/tools/servicenow.hocon` (copy `RequestReader`, change `name` and `entity`) | no |
| Change which fields are readable / writable | `entities.<name>.read_fields` / `.write_fields` | no |
| Make a table read-only | Set its `write_fields` to `[]` | no |
| Fix wrong state/priority wording in answers | `entities.<name>.coded_fields` — code→label maps from the instance's choice lists | no |
| Serve the network | `registries/tools/manifest.hocon`: flip `"tools/servicenow.hocon"` to `true` | no |
| Change credentials | Locally: `SN_CLIENT_ID` / `SN_CLIENT_SECRET` in `.env` (see `.env.example`). Cluster: the Secret behind the same names (+ `SN_GW_CREDENTIAL` for the variant flow) | no |
| **Gateway needs an extra per-call header** (e.g. an API key id) | `auth.extra_headers` in the profile: `{ "keyId": "env:SN_KEY_ID" }`, and set `SN_KEY_ID` in `.env` / the Secret. `env:NAME` keeps the value out of the profile | no |
| Send a literal `=` (or other char) in query values | `query_safe_chars` in the profile (default `"="`). Some custom parsers reject `%3D`; widen to e.g. `"=^"` for compound queries, `""` for strict encoding | no |
| Rotate gate signing keys | `SN_GATE_KEYS` = comma-separated list; **new key first**, old keys stay until tokens expire (5 min) | no |
| Switch token flow after `--try-both` | `auth.style`: `standard` or `preencoded` | no |
| Hotfix one profile key without reissuing the file | Env var, e.g. `SN_PROFILE__operations__read__path=/new/{table}` | no |
| Let a low-risk field skip approval | `gate.auto_approve_fields` (empty by default — opt-in) | no |
| Enable record creation | `operations.create.enabled: true` — **only after the endpoint is confirmed in writing** | no |
| **Add attachment upload/download** | Code: new shape subclass + transport support. These shapes are **declared but not built** | **yes** |
| Change validation, gate, retry or audit behaviour | Code — see §4 to find the right file | yes |

After any profile change: **restart the process.** The profile is cached at
first use; a broken profile then refuses to start and names the offending key.

---

## 3a. Onboard your endpoints (worksheet)

Endpoints are defined **entirely in the profile** — the code supplies the *shapes*,
the profile supplies the *destinations*. Adding one never edits Python. Work through
your endpoint inventory row by row:

**Step 1 — pick the shape** for each endpoint from its URL/method:

| The endpoint looks like… | shape | Goes in |
|---|---|---|
| `GET …/<namespace>/<table>?<params>` | `query` | `operations.<name>` + one `entities.<name>` per table |
| `PUT`/`POST …/<namespace>/<table>` with a JSON body | `body` | `operations.<name>` (writes are gated) |
| attachment download (binary) / upload (multipart) / a publish action | *not built yet* | needs code — out of scope for now |

**Step 2 — add the operation** (once per namespace/method):

```json
"operations": {
  "read":   { "method":"GET",  "path":"/<read-namespace>/{table}",  "shape":"query",
              "query_params":["display","query"] },
  "update": { "method":"<verb>", "path":"/<update-namespace>/{table}", "shape":"body" }
}
```

- `{table}` is filled from the entity. Use `{record}` too if the identifier goes in
  the path. A fixed path with no `{table}` is fine for endpoints that don't target a
  table.
- **`query_params` is the fix for picky gateways** — list only the params the endpoint
  accepts. Sending more can make a gateway error. Field allow-listing still holds even
  when `fields` isn't sent (it's applied to the response).

**Step 3 — add one entity per table** you're permitted to touch:

```json
"entities": {
  "<logical-name>": {
    "table": "<real-table>",
    "display_field": "number",        // the value users refer to a record by ("name" for CMDB-style)
    "read_fields":  ["sys_id","number","..."],
    "write_fields": ["work_notes"],   // empty = read-only
    "coded_fields": { "state": { "1":"<label>", "2":"<label>" } }
  }
}
```

**Step 4 — add an agent** in `registries/tools/servicenow.hocon` only for entities a
user should be able to invoke (copy `RequestReader`, change `name` + `entity`).

**Rule of thumb:** new table → entity entry; new endpoint of a known shape → operation
entry (+ agent if user-facing); new *shape* → code. The real host, paths and tables go
only in your gitignored `sn_profile.json`, never in the repo.

---

## 4. How the Python files work together

One request flows through the modules in this order:

```
registries/tools/servicenow.hocon  agent pins {operation, entity} — logic only
        │
        ▼
query_records.py / propose_change.py / commit_change.py / create_record.py
        │            (thin tool classes — WHAT each tool does)
        ▼
base.py            the shared pipeline every tool runs:
        │            validate fields → ground the record reference → gate check
        │            → call the gateway → report. Tools override run(); the
        │            pipeline order is fixed here so no tool can skip a control.
        ├──▶ context.py     correlation id + the sly_data key names (sn_*)
        ├──▶ profile.py     loads/validates the config file (once, cached)
        ├──▶ router.py      (operation, entity) + profile → method + full URL
        ├──▶ labels.py      codes ↔ labels, both directions
        ├──▶ gate.py        mint/verify the HMAC approval token
        ▼
transport.py       the ONLY file that talks HTTP: worker thread, timeouts,
        │            retry (reads only), circuit breaker, error scrubbing
        ├──▶ auth.py        bearer token: both flows + process-wide cache
        ├──▶ scrub.py       redacts secrets/hosts from anything outbound
        ▼
reporting.py       every decision → audit line (logs) + progress frame (UI)
errors.py          typed errors with stable reason codes, used by all of the above
```

Rules of thumb when editing:

- **Tool files are thin on purpose.** If you're adding logic to
  `query_records.py` that another tool would also need, it belongs in `base.py`.
- **Only `transport.py` may perform HTTP.** Auth, retries and scrubbing are
  provable because there is exactly one exit.
- **Only `profile.py` may read config.** Everything else receives values.
- Every module's docstring states *why it exists*; read it before changing it.
- Tests mirror the layout, plus `test_review_findings.py` (R1–R5) and
  `test_fundamentals.py` (F1–F3) — one regression test per past defect.
  **If you fix a bug, add its test there.**

---

## 5. Where the logs are

| Where | What | Survives? |
|---|---|---|
| `logs/server.log` (local) / pod stdout (cluster) | Framework journal **and every `servicenow_*` audit line** | Local: yes. Cluster: **only if log shipping exists** — open item |
| UI / API stream | Live progress markers — **only if the client sends `chat_filter: MAXIMAL`**; default clients get just the final answer | Not saved |
| `logs/thinking_dir/` | Per-agent LLM transcripts (debug aid) | Local only |

Audit lines are **logfmt** (`key=value`, percent-encoded values), not JSON — the
framework wraps log messages in an envelope without escaping, so JSON there is
unparseable; logfmt survives any wrapper and Splunk-style extraction reads it
natively. Grep by marker or correlation id (every tool answer and every
connection-check run prints one):

```
grep servicenow_change_verified logs/server.log
grep <correlation_id> logs/server.log
```

Each line also carries the framework's own `request_id` (joins to its journal)
and `user_id`. Field names are always logged; values only if allow-listed;
never a token or credential.

### Debugging URL stitching — `SN_DEBUG=1`

The audit lines above deliberately omit the host and full URL (a hostname is a
deployment identifier). That is what makes "which URL did it actually hit?" hard to
see. For that, turn on the **opt-in debug channel**:

```bash
SN_DEBUG=1 python coded_tools/tools/servicenow/check_connection.py --entity <name> --record <ref>
# or, in the running server / pod: set SN_DEBUG=1 in the environment
```

It emits, to stderr, the full picture at each composition point — no code edit,
no hand-added prints:

```
[sn-debug] …auth:      token exchange: POST <token_url> style=standard content_type=…  -> HTTP 200
[sn-debug] …router:    stitch read/incident: base=<base> + path='/…/{table}' (table=incident) -> <full url> | shape=query gated=False query_params=['display','query']
[sn-debug] …transport: PING GET <url> params={…} headers=['Accept','Authorization']
[sn-debug] …transport: PONG HTTP 200 <- <FULLY STITCHED URL incl. ?sysparm_…>
```

The `PONG` line is the exact wire URL, query string and all — the thing you had to
hand-print before. It shows header **names** only, never a token or credential.

**Caveat, deliberately stated:** these lines contain real hostnames and URLs. Turn
`SN_DEBUG` on only while troubleshooting, and don't point DEBUG at a log sink that
leaves the host. Default (off) keeps the shipped audit stream host-free.

---

## 6. Rules that must not be broken

These come from framework facts and past incidents (see the R*/F* register tests):

1. **No secret ever travels in tool `args`** — the framework journals args
   verbatim into the chat. Secrets: environment only. Approval token: sly_data
   only.
2. **No endpoint/host/table/client name in code or registry** — enforced by
   `test_no_client_identifiers.py`, which scans this README too. Deliberate
   exceptions are per-line `scrub-allow` markers. Run it before committing:
   `pytest -o addopts= tests/coded_tools/tools/servicenow/test_no_client_identifiers.py`.
3. **No blocking calls on the event loop** — all HTTP via `asyncio.to_thread`
   (a slow-gateway liveness test enforces it).
4. **Writes are never auto-retried** — an async create with no reference
   duplicates records. Reads retry; failed writes return their correlation id
   for a deliberate confirm-then-retry.
5. **`SN_GATE_KEYS` must be shared across replicas** — never per-pod: propose on
   one pod, commit on another, and verification fails. No key = gated writes
   refuse (by design).
6. **Cross-turn tests build sly_data from the released keys only**
   (`sn_commit_token`, `sn_pending_change`, `sn_correlation_id`,
   `sn_record_ids`). An in-process shortcut once hid a real two-turn failure
   (finding R2).

---

## 7. Current limits

- `create` ships **disabled** until its endpoint is confirmed in writing.
- Attachment shapes (`BINARY`, `MULTIPART`) are declared but not built.
- `auth.style` needs one `--try-both` run per environment to settle.
- Cluster audit is inert until the platform team stands up log shipping.
- Extracting the generic layers into a shared chassis for other integrations is
  deliberately deferred until a second integration exists.
