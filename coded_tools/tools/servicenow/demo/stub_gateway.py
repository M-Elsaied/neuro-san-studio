"""
A local stand-in for the gateway, speaking real HTTP.

The in-memory FakeGateway proves the package's logic. This proves the parts logic
cannot: that the token exchange works over a socket, that headers and query encoding
survive the wire, that the retry and timeout budget behave against a real server, and
that every blocking call really does leave the event loop.

Standard library only, on an ephemeral port, so the whole thing runs on a laptop with
no dependency to install, no credentials, and no access to any real system.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import parse_qs
from urllib.parse import urlparse

TOKEN_PATH: str = "/oauth/token"
READ_PREFIX: str = "/read/"
UPDATE_PREFIX: str = "/update/"

ISSUED_TOKEN: str = "stub-issued-token"


class _Handler(BaseHTTPRequestHandler):
    """Serves the three endpoints the tools actually use."""

    # HTTP/1.1 so responses are keep-alive rather than close-per-request. The
    # default HTTP/1.0 tears down a connection for every call, and a suite making a
    # few hundred of them will occasionally hit a reset on Windows. Every response
    # here sends Content-Length, which 1.1 requires for keep-alive to be correct.
    protocol_version = "HTTP/1.1"

    # Injected by StubGateway before the server starts.
    state: Dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log during tests."""

    def _respond(self, status: int, payload: Any) -> None:
        """
        :param status: HTTP status code.
        :param payload: JSON-serializable body.
        """
        body: bytes = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        """
        :return: The parsed request body, or an empty dictionary.
        """
        length: int = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw: bytes = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return {key: value[0] for key, value in parse_qs(raw.decode("utf-8")).items()}

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Handle the token exchange."""
        path: str = urlparse(self.path).path
        self.state.setdefault("requests", []).append(
            {"method": "POST", "path": path, "headers": dict(self.headers)})

        if path == TOKEN_PATH:
            if not self.headers.get("Authorization", "").startswith("Basic "):
                self._respond(401, {"error": "invalid_client"})
                return
            self._respond(200, {"access_token": ISSUED_TOKEN, "token_type": "Bearer",
                                "expires_in": 3600})
            return
        self._respond(404, {"error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Handle a record update."""
        path: str = urlparse(self.path).path
        body: Dict[str, Any] = self._body()
        self.state.setdefault("requests", []).append(
            {"method": "PUT", "path": path, "headers": dict(self.headers), "body": body})

        if not self._authorized():
            return
        if not path.startswith(UPDATE_PREFIX):
            self._respond(404, {"error": "not_found"})
            return

        records: Dict[str, Dict[str, Any]] = self.state["records"]
        record_id: Optional[str] = body.get("sys_id")
        if record_id not in records:
            self._respond(404, {"error": "no_record"})
            return
        records[record_id].update({key: value for key, value in body.items()
                                   if key != "sys_id"})
        self.state.setdefault("writes", []).append(dict(body))
        self._respond(200, {"result": {"sys_id": record_id}})

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Handle a record query."""
        parsed = urlparse(self.path)
        params: Dict[str, List[str]] = parse_qs(parsed.query)
        self.state.setdefault("requests", []).append(
            {"method": "GET", "path": parsed.path, "params": params,
             "headers": dict(self.headers)})

        if not self._authorized():
            return
        if not parsed.path.startswith(READ_PREFIX):
            self._respond(404, {"error": "not_found"})
            return

        matches: List[Dict[str, Any]] = list(self.state["records"].values())
        query: str = (params.get("sysparm_query") or [""])[0]
        if "=" in query:
            field, _, wanted = query.partition("=")
            matches = [record for record in matches if str(record.get(field)) == wanted]

        limit: int = int((params.get("sysparm_limit") or [str(len(matches))])[0])
        offset: int = int((params.get("sysparm_offset") or ["0"])[0])
        fields: List[str] = [name for name in
                             (params.get("sysparm_fields") or [""])[0].split(",") if name]
        window = matches[offset:offset + limit]
        if fields:
            window = [{name: record.get(name) for name in fields if name in record}
                      for record in window]

        # Emulate display-value rendering. A real gateway asked for display values
        # returns the human label, not the stored code. Without this the stub was
        # more forgiving than the thing it stands in for, and hid the very class of
        # failure it exists to surface.
        if (params.get("sysparm_display_value") or ["false"])[0].lower() == "true":
            display_map: Dict[str, Dict[str, str]] = self.state.get("display_values") or {}
            window = [
                {name: display_map.get(name, {}).get(str(value), value)
                 for name, value in record.items()}
                for record in window
            ]
        self._respond(200, {"result": window})

    def _authorized(self) -> bool:
        """
        :return: True when a bearer token was presented; responds 401 otherwise.
        """
        header: str = self.headers.get("Authorization", "")
        if header == f"Bearer {ISSUED_TOKEN}":
            return True
        self._respond(401, {"error": "unauthorized"})
        return False


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """
    A stub server that refuses to share its port.

    ``HTTPServer`` sets ``allow_reuse_address = 1``, and on Windows SO_REUSEADDR
    means something stronger than it does elsewhere: a socket may bind a port
    another socket is still holding. Across a suite that starts one stub per test,
    that let a request land on the previous, already-shutting-down server — an
    intermittent 401 in roughly one run in ten. Turning it off forces the operating
    system to hand out a genuinely free port.
    """

    allow_reuse_address = False
    daemon_threads = True


class StubGateway:
    """A running stub gateway, usable as a context manager."""

    def __init__(self, records: Optional[Dict[str, Dict[str, Any]]] = None, port: int = 0,
                 display_values: Optional[Dict[str, Dict[str, str]]] = None):
        """
        :param records: Synthetic records to serve.
        :param port: Port to bind. The default of 0 lets the operating system pick a
                     free one, so parallel test runs never collide; the demo runner
                     passes a fixed port so a profile can name it.
        :param display_values: {field: {code: label}} used to emulate a gateway that
                               honours display-value requests.
        """
        self.state: Dict[str, Any] = {"records": dict(records or {}),
                                      "display_values": dict(display_values or {}),
                                      "requests": [], "writes": []}
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self.server = _ExclusiveHTTPServer(("127.0.0.1", port), handler)  # scrub-allow: loopback
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """:return: The chosen port."""
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        """
        :return: The stub's base URL.

        Addressed by loopback literal rather than by the name "localhost": on
        Windows that name resolves to the IPv6 address first, and since the stub
        binds IPv4 only, every request pays a connection timeout before falling
        back. It turned a one-second suite into a forty-second one.
        """
        return f"http://127.0.0.1:{self.port}"  # scrub-allow: loopback literal

    def profile_document(self) -> Dict[str, Any]:
        """
        :return: A profile document wired to this stub, structurally identical to a
                 real deployment profile — only the destinations differ.
        """
        return {
            "base_url": self.base_url,
            "auth": {
                "style": "standard",
                "token_url": f"{self.base_url}{TOKEN_PATH}",
                # Deliberately the package defaults, so ONE .env serves the demo,
                # the checker, the server and (via Secret) the cluster alike.
                "client_id_env": "SN_CLIENT_ID",
                "client_secret_env": "SN_CLIENT_SECRET",
            },
            "operations": {
                "read": {"method": "GET", "path": "/read/{table}", "shape": "query"},
                "update": {"method": "PUT", "path": "/update/{table}", "shape": "body"},
            },
            "entities": {
                "request": {
                    "table": "stub_request_table",
                    "identifier_field": "sys_id",
                    "read_fields": ["sys_id", "number", "short_description", "state"],
                    "write_fields": ["work_notes", "state"],
                }
            },
            "limits": {"default_page": 5, "max_page": 10, "max_retries": 1,
                       "backoff_base_seconds": 0.0, "timeout_seconds": 5,
                       "connect_timeout_seconds": 5},
            "gate": {"keys_env": "SN_GATE_KEYS", "ttl_seconds": 300},
            "correlation_field": "u_correlation",
        }

    def __enter__(self) -> "StubGateway":
        self.thread.start()
        return self

    def __exit__(self, *exception: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
