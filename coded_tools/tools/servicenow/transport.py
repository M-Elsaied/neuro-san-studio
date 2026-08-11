"""
The one HTTP seam.

Everything this package sends leaves through here, which is what makes the
resilience and leak-proofing properties provable rather than aspirational, and what
would make a later lift into an MCP server a wrapper instead of a rewrite.

Non-negotiables enforced in this module:

  * **Nothing blocks the event loop.** The framework's own CodedTool docstring warns
    that a synchronous socket call "*will* block all other agent operations". Every
    blocking call — business request and token mint alike — runs inside
    ``asyncio.to_thread``.
  * **Writes are never retried automatically.** An asynchronous create that returns
    no reliable reference duplicates records under blind retry. Only operations
    marked idempotent in the router's spec are retried; a failed write comes back
    with its correlation id so a deliberate confirm-then-retry is possible.
  * **Error bodies are scrubbed before they are seen by anything.** An upstream
    error page can echo an Authorization header or an internal host.
  * **TLS verification is on.** There is no code path that disables it.
"""

import asyncio
import logging
import random
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional
from typing import Tuple

import requests

from coded_tools.tools.servicenow.auth import TOKEN_CACHE
from coded_tools.tools.servicenow.auth import build_provider
from coded_tools.tools.servicenow.auth import resolve_extra_headers
from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import AuthError
from coded_tools.tools.servicenow.errors import CircuitOpenError
from coded_tools.tools.servicenow.errors import TransportError
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.reporting import MARKER_AUTH
from coded_tools.tools.servicenow.reporting import MARKER_DOWNSTREAM_CALL
from coded_tools.tools.servicenow.reporting import report
from coded_tools.tools.servicenow.router import BoundRoute
from coded_tools.tools.servicenow.scrub import scrub_text

log = logging.getLogger(__name__)

RETRYABLE_STATUSES: frozenset = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS: float = 8.0


@dataclass(frozen=True)
class RawResponse:
    """A transport-level response, independent of any HTTP library."""

    status: int
    text: str
    json_body: Any = None
    headers: Mapping[str, str] = None


class HttpTransport:
    """Performs one blocking HTTP request. Swapped wholesale in tests."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def request(self, method: str, url: str, headers: Mapping[str, str],
                params: Optional[Mapping[str, Any]], json_body: Optional[Mapping[str, Any]],
                timeout: Tuple[float, float], verify: bool) -> RawResponse:
        """
        :param method: HTTP method.
        :param url: Absolute URL.
        :param headers: Request headers.
        :param params: Query-string parameters, or None.
        :param json_body: JSON request body, or None.
        :param timeout: (connect, read) timeout pair.
        :param verify: TLS verification flag; always True here.
        :return: The raw response.
        """
        raise NotImplementedError


class RequestsTransport(HttpTransport):
    """The production transport, using the coded-tool house library."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def request(self, method: str, url: str, headers: Mapping[str, str],
                params: Optional[Mapping[str, Any]], json_body: Optional[Mapping[str, Any]],
                timeout: Tuple[float, float], verify: bool) -> RawResponse:
        # Header NAMES only (never values — an Authorization value is a credential).
        log.debug("PING %s %s params=%s headers=%s", method, url,
                  dict(params or {}), sorted(headers))
        try:
            response = requests.request(method, url, headers=dict(headers), params=params,
                                        json=json_body, timeout=timeout, verify=verify)
        except requests.RequestException as exception:
            log.debug("PING failed %s %s: %s", method, url, type(exception).__name__)
            raise TransportError(f"Downstream unreachable: {type(exception).__name__}",
                                 reason="downstream_unreachable") from exception
        # The fully stitched URL, query string and all — the exact wire target.
        log.debug("PONG HTTP %s <- %s", response.status_code, response.request.url)
        parsed: Any = None
        if response.content:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
        return RawResponse(status=response.status_code, text=response.text or "",
                           json_body=parsed, headers=dict(response.headers))


class CircuitBreaker:
    """
    A small per-route breaker whose only job is to stop a failing dependency from
    tying up worker threads behind full-length timeouts.

    Per-process state is the right scope: this protects *this* pod's loop, and
    coordinating it globally would add shared state for no additional safety.
    """

    def __init__(self, threshold: int, reset_seconds: float):
        """
        :param threshold: Consecutive failures before the circuit opens.
        :param reset_seconds: How long to stay open before allowing a trial call.
        """
        self.threshold: int = threshold
        self.reset_seconds: float = reset_seconds
        self._lock = threading.Lock()
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}

    def check(self, key: str, now: Optional[float] = None) -> None:
        """
        :param key: The logical route key.
        :param now: Epoch seconds; defaults to time.time().
        :raises CircuitOpenError: while the circuit for this route is open.
        """
        moment: float = time.time() if now is None else now
        with self._lock:
            opened: Optional[float] = self._opened_at.get(key)
            if opened is None:
                return
            if moment - opened < self.reset_seconds:
                raise CircuitOpenError(
                    "The downstream route is failing repeatedly and calls are paused "
                    "briefly. Retry shortly.", route=key)
            # Half-open: allow one trial call through.
            self._opened_at.pop(key, None)
            self._failures[key] = self.threshold - 1

    def record_success(self, key: str) -> None:
        """:param key: The logical route key."""
        with self._lock:
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)

    def record_failure(self, key: str, now: Optional[float] = None) -> None:
        """
        :param key: The logical route key.
        :param now: Epoch seconds; defaults to time.time().
        """
        moment: float = time.time() if now is None else now
        with self._lock:
            count: int = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.threshold:
                self._opened_at[key] = moment

    def reset(self) -> None:
        """Clear all breaker state."""
        with self._lock:
            self._failures.clear()
            self._opened_at.clear()


# Keyed by the loop *object*, weakly. An asyncio primitive is bound to the loop that
# created it, so the cache must be per loop — but keying by id() is unsafe: CPython
# reuses an id once the loop is collected, and a later loop can then pick up a
# semaphore belonging to a dead one, which deadlocks. Weak keys make entries vanish
# with their loop instead.
_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = \
    weakref.WeakKeyDictionary()
_SEMAPHORE_LOCK = threading.Lock()


def _semaphore(limit: int) -> asyncio.Semaphore:
    """
    Return the outbound-concurrency semaphore for the running event loop.

    :param limit: Maximum simultaneous outbound calls.
    :return: The semaphore.
    """
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    with _SEMAPHORE_LOCK:
        existing: Optional[asyncio.Semaphore] = _SEMAPHORES.get(loop)
        if existing is None:
            existing = asyncio.Semaphore(limit)
            _SEMAPHORES[loop] = existing
        return existing


def reset_transport_state() -> None:
    """Clear breaker and semaphore state. Intended for tests."""
    BREAKER.reset()
    with _SEMAPHORE_LOCK:
        _SEMAPHORES.clear()
    TOKEN_CACHE.invalidate()


BREAKER = CircuitBreaker(threshold=5, reset_seconds=30.0)
TRANSPORT: HttpTransport = RequestsTransport()


@dataclass(frozen=True)
class GatewayResult:
    """A successful downstream call."""

    status: int
    body: Any
    latency_ms: int
    attempts: int


def _backoff_seconds(attempt: int, base: float) -> float:
    """
    :param attempt: 1-based attempt number that just failed.
    :param base: Base delay in seconds.
    :return: Jittered exponential delay, capped.
    """
    ceiling: float = min(base * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
    return random.uniform(ceiling / 2.0, ceiling)


def _retry_after(response: RawResponse) -> Optional[float]:
    """
    :param response: The raw response.
    :return: The server's requested delay, when it supplied a usable one.
    """
    header: str = str((response.headers or {}).get("Retry-After", "")).strip()
    if not header:
        return None
    try:
        return max(0.0, min(float(header), MAX_BACKOFF_SECONDS))
    except ValueError:
        return None


class Gateway:
    """Issues authenticated, resilient calls against bound routes."""

    def __init__(self, profile: Profile, transport: Optional[HttpTransport] = None):
        """
        :param profile: The deployment profile.
        :param transport: Override for the HTTP performer; tests inject a fake.
        """
        self.profile: Profile = profile
        self.transport: HttpTransport = transport or TRANSPORT
        self.breaker: CircuitBreaker = BREAKER
        self.breaker.threshold = profile.limits.breaker_threshold
        self.breaker.reset_seconds = profile.limits.breaker_reset_seconds

    def _headers(self, token: str) -> Dict[str, str]:
        """
        :param token: The bearer token value.
        :return: Headers for a business call, including any profile-supplied extras.
        """
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        headers.update(resolve_extra_headers(self.profile.auth))
        return headers

    def _token(self) -> Tuple[str, str, Optional[float]]:
        """
        :return: (token value, action, seconds to expiry). Blocking.
        """
        return TOKEN_CACHE.get(build_provider(self.profile))

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def _attempt(self, route: BoundRoute, params: Optional[Mapping[str, Any]],
                 body: Optional[Mapping[str, Any]],
                 timeout: Tuple[float, float]) -> Tuple[RawResponse, str, Optional[float]]:
        """
        Perform one authenticated attempt. Blocking; runs on a worker thread.

        :param route: The bound route.
        :param params: Query parameters, or None.
        :param body: JSON body, or None.
        :param timeout: (connect, read) timeouts.
        :return: (response, token action, token ttl)
        """
        token, action, ttl = self._token()
        response: RawResponse = self.transport.request(
            route.method, route.url, self._headers(token), params, body,
            timeout, self.profile.verify_tls)

        if response.status == 401:
            # The cached token was revoked or expired early. Drop it and try once
            # more with freshly minted credentials. A 401 means the request was
            # never processed, so this is safe even for a write.
            TOKEN_CACHE.invalidate()
            token, action, ttl = self._token()
            response = self.transport.request(
                route.method, route.url, self._headers(token), params, body,
                timeout, self.profile.verify_tls)
        return response, action, ttl

    async def call(self, route: BoundRoute, args: Mapping[str, Any], context: ToolContext,
                   params: Optional[Mapping[str, Any]] = None,
                   body: Optional[Mapping[str, Any]] = None) -> GatewayResult:
        """
        Issue a call, with retry, breaker and reporting applied.

        :param route: The bound route.
        :param args: Tool arguments, for the reporting rails.
        :param context: The invocation context.
        :param params: Query parameters, or None.
        :param body: JSON body, or None.
        :return: The successful result.
        :raises TransportError: on exhausted retries or a non-retryable failure.
        :raises AuthError: if a token could not be obtained.
        """
        limits = route.limits
        timeout: Tuple[float, float] = (limits.connect_timeout_seconds, limits.timeout_seconds)
        max_attempts: int = 1 + (limits.max_retries if route.spec.retryable else 0)

        self.breaker.check(route.route_key)

        started: float = time.perf_counter()
        last_status: Optional[int] = None
        last_detail: str = ""

        async with _semaphore(limits.max_concurrent):
            for attempt in range(1, max_attempts + 1):
                try:
                    # Every blocking operation, including the token mint inside
                    # _attempt, happens on a worker thread. This is the line that
                    # keeps one slow gateway from stalling every other agent.
                    response, token_action, token_ttl = await asyncio.to_thread(
                        self._attempt, route, params, body, timeout)
                except (TransportError, AuthError) as exception:
                    self.breaker.record_failure(route.route_key)
                    if isinstance(exception, AuthError) or attempt >= max_attempts:
                        raise
                    await asyncio.sleep(_backoff_seconds(attempt, limits.backoff_base_seconds))
                    continue

                latency_ms: int = int((time.perf_counter() - started) * 1000)
                await report(args, context, {
                    MARKER_DOWNSTREAM_CALL: True,
                    "method": route.method,
                    "route": route.route_key,
                    "status": response.status,
                    "latency_ms": latency_ms,
                    "attempt": attempt,
                    "token_action": token_action,
                })
                if token_action in ("minted", "refreshed"):
                    await report(args, context, {
                        MARKER_AUTH: True,
                        "action": token_action,
                        "style": self.profile.auth.style,
                        "expires_in_seconds": int(token_ttl) if token_ttl else None,
                    })

                if 200 <= response.status < 300:
                    self.breaker.record_success(route.route_key)
                    return GatewayResult(status=response.status, body=response.json_body,
                                         latency_ms=latency_ms, attempts=attempt)

                last_status = response.status
                last_detail = scrub_text(response.text, limit=300)

                if response.status in RETRYABLE_STATUSES and attempt < max_attempts:
                    self.breaker.record_failure(route.route_key)
                    delay: float = (_retry_after(response)
                                    or _backoff_seconds(attempt, limits.backoff_base_seconds))
                    await asyncio.sleep(delay)
                    continue

                if response.status >= 500 or response.status == 429:
                    self.breaker.record_failure(route.route_key)
                else:
                    # A 4xx is the caller's problem, not a sick dependency; it must
                    # not push the breaker toward opening for everyone else.
                    self.breaker.record_success(route.route_key)
                break

        raise TransportError(
            f"Downstream returned HTTP {last_status}: {last_detail}"
            if last_detail else f"Downstream returned HTTP {last_status}.",
            reason="downstream_failed", status_code=last_status,
            correlation_id=context.correlation_id)
