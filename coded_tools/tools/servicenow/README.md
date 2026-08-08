# ServiceNow tool family for neuro-san

Agents **read** records in a ServiceNow-style system through an API gateway, and
**change** them only after a person approves the exact change. All endpoints,
tables and credentials come from a config file at runtime — the code contains
none, and a test fails the build if one appears.

- Review registers travel as tests: `test_review_findings.py` (R1–R5), `test_fundamentals.py` (F1–F3)
- 150 tests, ~8 s, no credentials, no network beyond loopback
- Requires `neuro-san>=0.6` (this repo's pin qualifies)

---

## 1. What do you want to do?

**Start here.** Almost everything is a config change. If your task is in this
table, do exactly what it says and touch nothing else.

| I want to… | Change this | Python? |
|---|---|---|
| **Prove the connection works, before anything agent-shaped** | `PYTHONPATH=. python coded_tools/tools/servicenow/check_connection.py` — profile → credentials → token → one read, stopping at the first failure with a named remedy. `--try-both` reports which auth flow the gateway accepts; `--record REQ...` looks up one record. Needs no server, no LLM key. | no |
| Run the tests | `pytest -o addopts= tests/coded_tools/tools/servicenow -q` | no |
| Run the local demo | §3 below — two commands | no |
| **Point at a real gateway** | Copy `coded_tools/tools/servicenow/profile.example.json`, fill it, mount it, set `SN_PROFILE_FILE` to its path | **no** |
| Change an endpoint path / HTTP method | `operations.<name>.path` / `.method` in the profile | no |
| Put one operation on a different host or API version | `operations.<name>.base_url` in the profile | no |
| Add a table the agents may touch | Add an `entities.<name>` block in the profile **+** one agent block in `registries/tools/servicenow.hocon` (copy `RequestReader`, change `name` and `entity`) | no |
| Change which fields are readable / writable | `entities.<name>.read_fields` / `.write_fields` | no |
| Make a table read-only | Set its `write_fields` to `[]` | no |
| Fix wrong state/priority wording in answers | `entities.<name>.coded_fields` — code→label maps from the instance's choice lists | no |
| Change credentials | The Secret behind `SN_CLIENT_ID` / `SN_CLIENT_SECRET` (and `SN_GW_CREDENTIAL` for the variant flow) | no |
| Rotate gate signing keys | `SN_GATE_KEYS` = comma-separated list; **new key first**, old keys stay until tokens expire (5 min) | no |
| Switch token flow after the gateway test call | `auth.style`: `standard` or `preencoded` | no |
| Hotfix one profile key without reissuing the file | Env var, e.g. `SN_PROFILE__operations__read__path=/new/{table}` | no |
| Let a low-risk field skip approval | `gate.auto_approve_fields` (empty by default — opt-in) | no |
| Enable record creation | `operations.create.enabled: true` — **only after the endpoint is confirmed in writing** | no |
| **Add attachment upload/download** | Code: new shape subclass + transport support. These shapes are **declared but not built** | **yes** |
| Change validation, gate, retry or audit behaviour | Code — see §2 to find the right file | yes |

After any profile change: **restart the process.** The profile is cached at first
use; a broken profile then refuses to start and names the offending key.

---

## 2. How the Python files work together

One request flows through the modules in this order:

```
registries/tools/servicenow.hocon        agent pins {operation, entity} — logic only
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
- Tests mirror the layout: `tests/coded_tools/tools/servicenow/test_<area>.py`, plus
  `test_review_findings.py` (R1–R5) and `test_fundamentals.py` (F1–F3) — one
  regression test per past defect. **If you fix a bug, add its test there.**

---

## 3. Run the local demo

Terminal 1 — stub gateway (fake ServiceNow, port 8099; also writes the demo
profile to `coded_tools/tools/servicenow/demo/profile.local.json`, gitignored):

```bash
PYTHONPATH=. python coded_tools/tools/servicenow/demo/run_stub_gateway.py
```

Terminal 2 — server + UI (PowerShell, repo root; UI on 4183, API on 8123):

```powershell
$root = (Get-Location).Path
Remove-Item Env:\OPENAI_API_KEY -ErrorAction SilentlyContinue   # gotcha 3
$env:PYTHONPATH          = $root
$env:AGENT_TOOL_PATH     = "$root\coded_tools"                  # gotcha 2
$env:AGENT_MANIFEST_FILE = "$root\registries\manifest.hocon"
$env:SN_PROFILE_FILE     = "$root\coded_tools\tools\servicenow\demo\profile.local.json"
$env:SN_DEMO_CLIENT_ID     = "demo-id"
$env:SN_DEMO_CLIENT_SECRET = "demo-secret"
$env:SN_DEMO_GATE_KEYS     = "demo-signing-key"
python run.py     # this repo's launcher; see its --help for port options
```

Needs a funded LLM key in `.env` (`OPENAI_API_KEY=...`).

### Gotchas — read before debugging anything

1. **HOCON types:** the framework accepts `"int"`/`"float"`, **not** `"integer"`.
   One wrong type silently drops the whole network; the only symptom is an empty
   agent list.
2. **`AGENT_TOOL_PATH` needs backslashes on Windows.** Forward slashes give a
   runtime "Could not find class", not a startup error.
3. **A key already in your shell beats `.env`** — the launcher's dotenv does not
   override. Unset stale keys first.
4. Port 8080 is often taken and the launcher's conflict prompt is interactive —
   pick free ports up front.

---

## 4. Where the logs are

| Where | What | Survives? |
|---|---|---|
| `logs/server.log` (local) / pod stdout (cluster) | Framework journal **and every `servicenow_*` audit line** | Local: yes. Cluster: **only if log shipping exists** — open item |
| UI / API stream | Live progress markers — **only if the client sends `chat_filter: MAXIMAL`**; default clients get just the final answer | Not saved |
| `logs/thinking_dir/` | Per-agent LLM transcripts (debug aid) | Local only |

Audit lines are **logfmt** (`key=value`, percent-encoded values), not JSON — the
framework wraps log messages in an envelope without escaping, so JSON there is
unparseable; logfmt survives any wrapper and Splunk-style extraction reads it
natively. Grep by marker or correlation id:

```
grep servicenow_change_verified logs/server.log
grep <correlation_id> logs/server.log        # the id every tool answer includes
```

Each line also carries the framework's own `request_id` (joins to its journal)
and `user_id`. Field names are always logged; values only if allow-listed;
never a token or credential.

---

## 5. Rules that must not be broken

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

## 6. Current limits

- `create` ships **disabled** until its endpoint is confirmed in writing.
- Attachment shapes (`BINARY`, `MULTIPART`) are declared but not built.
- `auth.style` needs one live token call per environment to settle.
- Cluster audit is inert until the platform team stands up log shipping.
- Extracting the generic layers into a shared chassis for other integrations is
  deliberately deferred until a second integration exists.

The full design history (gap registers G1–G12, R1–R5, F1–F3 with reasoning) lives
with the originating engagement workspace; the enforceable parts travel here as
tests.
