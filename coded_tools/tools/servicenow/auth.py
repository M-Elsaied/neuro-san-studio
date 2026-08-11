"""
Token acquisition.

Two mutually incompatible client-credentials flows exist in the field and the
recorded evidence does not settle which a given gateway requires:

  * ``standard``   — RFC 6749 §4.4 with §2.3.1 client authentication. Basic header
                     built from id and secret; body carries grant_type (+scope).
  * ``preencoded`` — an opaque, pre-encoded Basic credential supplied as its own
                     secret, with the id and secret *also* repeated in the body,
                     the secret under a gateway-specific key name; form-encoded
                     first, JSON on a 400/415.

Rather than guess, both ship behind one interface and a profile key selects. One
live token call decides per environment, and the answer is configuration.

Nothing here logs a credential, a Basic header or a token value — only lifecycle.
All functions in this module block and are expected to run on a worker thread.
"""

import base64
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional
from typing import Tuple

import logging

import requests

from coded_tools.tools.servicenow.errors import AuthError
from coded_tools.tools.servicenow.profile import AuthConfig
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.scrub import scrub_text

log = logging.getLogger(__name__)

# Refresh this far ahead of stated expiry so a slow call cannot race the boundary.
REFRESH_SKEW_SECONDS: float = 60.0
DEFAULT_EXPIRES_IN: float = 3600.0
ENV_PREFIX: str = "env:"

# Transport-level attempts at the token endpoint. A mint is idempotent, so a
# dropped connection is safe to repeat; a rejection is not retried at all.
TOKEN_TRANSPORT_ATTEMPTS: int = 3
TOKEN_RETRY_BACKOFF_SECONDS: float = 0.2


@dataclass(frozen=True)
class Token:
    """A bearer token and the wall-clock time it stops being usable."""

    value: str
    expires_at: float

    def is_fresh(self, now: Optional[float] = None) -> bool:
        """
        :param now: Current epoch seconds; defaults to time.time().
        :return: True while the token remains usable allowing for refresh skew.
        """
        moment: float = time.time() if now is None else now
        return moment < (self.expires_at - REFRESH_SKEW_SECONDS)


def _require_env(name: str, purpose: str) -> str:
    """
    :param name: Environment variable name holding the secret.
    :param purpose: What it is needed for, used in the error message.
    :return: The value.
    :raises AuthError: naming the variable but never echoing any value.
    """
    value: Optional[str] = os.environ.get(name)
    if not value:
        raise AuthError(f"Environment variable '{name}' is required for {purpose} but is unset.",
                        reason="auth_misconfigured", missing_env=name)
    return value


def resolve_extra_headers(auth: AuthConfig) -> Dict[str, str]:
    """
    Resolve the profile's extra business-call headers.

    A value of ``env:NAME`` is read from the environment; anything else is literal.
    This covers gateway-specific per-call headers without any of them being named
    in this package.

    :param auth: The auth configuration.
    :return: Header name to value.
    """
    headers: Dict[str, str] = {}
    for name, value in auth.extra_headers.items():
        text: str = str(value)
        if text.startswith(ENV_PREFIX):
            headers[name] = _require_env(text[len(ENV_PREFIX):], f"header '{name}'")
        else:
            headers[name] = text
    return headers


class TokenProvider:
    """Interface for a client-credentials token exchange."""

    def __init__(self, auth: AuthConfig, verify_tls: bool, timeout: Tuple[float, float]):
        """
        :param auth: The auth configuration.
        :param verify_tls: Whether to verify the server certificate. Always True in
                           practice; the parameter exists so transport owns the policy.
        :param timeout: (connect, read) timeout pair.
        """
        self.auth: AuthConfig = auth
        self.verify_tls: bool = verify_tls
        self.timeout: Tuple[float, float] = timeout

    @property
    def style(self) -> str:
        """:return: The style name this provider implements."""
        raise NotImplementedError

    def _build_request(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        :return: A (headers, body) pair for the token request.
        """
        raise NotImplementedError

    def fetch_token(self) -> Token:
        """
        Perform the token exchange. Blocking; run on a worker thread.

        :return: A fresh Token.
        :raises AuthError: on any non-200, malformed body, or transport failure.
        """
        headers, body = self._build_request()
        attempts: Tuple[str, ...] = ("application/x-www-form-urlencoded",)
        if self.auth.style == "preencoded":
            # The variant flow is documented to negotiate: form first, JSON on 400/415.
            attempts = ("application/x-www-form-urlencoded", "application/json")

        last_status: Optional[int] = None
        last_detail: str = ""
        for content_type in attempts:
            request_headers: Dict[str, str] = dict(headers)
            request_headers["Content-Type"] = content_type
            request_headers["Accept"] = "application/json"
            payload: Dict[str, Any] = ({"json": body} if content_type == "application/json"
                                       else {"data": body})
            # Endpoint + style + content-type only — never the credential or token.
            log.debug("token exchange: POST %s style=%s content_type=%s",
                      self.auth.token_url, self.auth.style, content_type)
            response = self._post_with_retry(request_headers, payload)
            log.debug("token exchange: HTTP %s", response.status_code)

            if response.status_code == 200:
                return self._parse_token(response)

            last_status = response.status_code
            last_detail = scrub_text(response.text, limit=200)
            if response.status_code in (400, 415) and content_type != attempts[-1]:
                continue
            break

        raise AuthError(f"Token request rejected with HTTP {last_status}: {last_detail}",
                        reason="auth_rejected", status_code=last_status)

    def _post_with_retry(self, headers: Dict[str, str],
                         payload: Dict[str, Any]) -> requests.Response:
        """
        Post to the token endpoint, retrying transient transport failures.

        A token mint is idempotent, so retrying a dropped connection is safe — and
        necessary for parity: without it, a connection reset on the mint fails the
        whole tool call while the identical reset on a business call is retried.
        Only transport-level failures qualify. A rejection is not retried: repeating
        bad credentials cannot succeed and can lock an account.

        :param headers: Request headers.
        :param payload: Either a ``data`` or a ``json`` keyword payload.
        :return: The response.
        :raises AuthError: when the endpoint stays unreachable.
        """
        last: Optional[Exception] = None
        for attempt in range(1, TOKEN_TRANSPORT_ATTEMPTS + 1):
            try:
                return requests.post(self.auth.token_url, headers=headers,
                                     timeout=self.timeout, verify=self.verify_tls, **payload)
            except requests.RequestException as exception:
                last = exception
                if attempt < TOKEN_TRANSPORT_ATTEMPTS:
                    time.sleep(TOKEN_RETRY_BACKOFF_SECONDS * attempt)
        raise AuthError(f"Token endpoint unreachable: {type(last).__name__}",
                        reason="auth_unreachable") from last

    @staticmethod
    def _parse_token(response: requests.Response) -> Token:
        """
        :param response: A 200 response from the token endpoint.
        :return: The parsed Token.
        :raises AuthError: if the body is not JSON or carries no access token.
        """
        try:
            body: Mapping[str, Any] = response.json()
        except ValueError as exception:
            raise AuthError("Token endpoint returned a non-JSON body.",
                            reason="auth_malformed") from exception
        value: Optional[str] = body.get("access_token")
        if not value:
            # Never echo the body: a partial token may be present under another key.
            raise AuthError("Token endpoint response contained no access_token.",
                            reason="auth_malformed")
        try:
            expires_in: float = float(body.get("expires_in", DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError):
            expires_in = DEFAULT_EXPIRES_IN
        return Token(value=str(value), expires_at=time.time() + expires_in)


class StandardClientCredentials(TokenProvider):
    """
    RFC 6749 §4.4 client-credentials with §2.3.1 HTTP Basic client authentication.

    §2.3.1 requires the id and secret to be form-urlencoded before base64. That is
    a no-op for the common case of opaque hex-and-hyphen credentials, and it is
    what the specification says, so it is the default — but a gateway that
    predates or ignores the rule can switch it off in the profile.
    """

    @property
    def style(self) -> str:
        """:return: The style name."""
        return "standard"

    def _build_request(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        client_id: str = _require_env(self.auth.client_id_env, "the standard token exchange")
        client_secret: str = _require_env(self.auth.client_secret_env,
                                          "the standard token exchange")
        if self.auth.percent_encode_credentials:
            client_id = urllib.parse.quote(client_id, safe="")
            client_secret = urllib.parse.quote(client_secret, safe="")
        pair: str = f"{client_id}:{client_secret}"
        encoded: str = base64.b64encode(pair.encode("utf-8")).decode("ascii")

        headers: Dict[str, str] = {"Authorization": f"Basic {encoded}"}
        body: Dict[str, str] = {"grant_type": "client_credentials"}
        if self.auth.scope:
            body["scope"] = self.auth.scope
        return headers, body


class PreEncodedBasic(TokenProvider):
    """
    The variant flow: an opaque pre-encoded Basic credential is its own secret, and
    the id and secret are additionally carried in the body, the secret under a
    gateway-specific key name taken from the profile.
    """

    @property
    def style(self) -> str:
        """:return: The style name."""
        return "preencoded"

    def _build_request(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        credential: str = _require_env(self.auth.preencoded_credential_env,
                                       "the pre-encoded token exchange")
        headers: Dict[str, str] = {"Authorization": f"Basic {credential}"}
        body: Dict[str, str] = {"grant_type": "client_credentials"}
        if self.auth.send_id_in_body:
            body["client_id"] = _require_env(self.auth.client_id_env,
                                             "the pre-encoded token exchange")
        body[self.auth.secret_body_key] = _require_env(self.auth.client_secret_env,
                                                       "the pre-encoded token exchange")
        if self.auth.scope:
            body["scope"] = self.auth.scope
        return headers, body


_PROVIDERS: Mapping[str, type] = {
    "standard": StandardClientCredentials,
    "preencoded": PreEncodedBasic,
}


def build_provider(profile: Profile) -> TokenProvider:
    """
    :param profile: The deployment profile.
    :return: The TokenProvider for the configured style.
    """
    provider_class = _PROVIDERS[profile.auth.style]
    timeout: Tuple[float, float] = (profile.limits.connect_timeout_seconds,
                                    profile.limits.timeout_seconds)
    return provider_class(profile.auth, profile.verify_tls, timeout)


class TokenCache:
    """
    Process-wide bearer-token cache.

    A deliberate, documented exception to the framework's no-globals guidance. The
    framework warns against globals because per-request mutable state shared across
    requests harms concurrency. A bearer token is neither: it is immutable, has an
    hour-long lifetime, and is identical for every caller. The alternative is a
    network round trip on every single tool invocation.

    The lock is only taken on a miss, so the common path — a warm token — never
    contends. Because the whole call runs on a worker thread, a mint blocks that
    thread and never the event loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token: Optional[Token] = None
        self._style: Optional[str] = None
        self.last_action: str = "none"

    def get(self, provider: TokenProvider) -> Tuple[str, str, Optional[float]]:
        """
        Return a usable token, minting one only when necessary.

        :param provider: The provider to mint with on a miss.
        :return: (token value, action taken, seconds until expiry)
        """
        cached: Optional[Token] = self._token
        if cached is not None and self._style == provider.style and cached.is_fresh():
            self.last_action = "reused"
            return cached.value, "reused", cached.expires_at - time.time()

        with self._lock:
            # Re-check inside the lock: another thread may have minted while we waited.
            cached = self._token
            if cached is not None and self._style == provider.style and cached.is_fresh():
                self.last_action = "reused"
                return cached.value, "reused", cached.expires_at - time.time()
            action: str = "refreshed" if cached is not None else "minted"
            fresh: Token = provider.fetch_token()
            self._token = fresh
            self._style = provider.style
            self.last_action = action
            return fresh.value, action, fresh.expires_at - time.time()

    def invalidate(self) -> None:
        """Drop the cached token, forcing the next call to mint."""
        with self._lock:
            self._token = None


TOKEN_CACHE = TokenCache()
