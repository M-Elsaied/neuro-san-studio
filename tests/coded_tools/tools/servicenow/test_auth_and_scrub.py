"""
Token acquisition and outbound text scrubbing.

The auth tests assert the exact wire shape each strategy produces. That matters
because the two flows are mutually incompatible and the evidence does not settle
which a given gateway wants: when the answer arrives, these tests are what proves
the selected style still sends what it is supposed to.
"""

import base64
import os
import time
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional
from typing import Tuple
from unittest import TestCase
from unittest.mock import patch

from coded_tools.tools.servicenow.auth import PreEncodedBasic
from coded_tools.tools.servicenow.auth import StandardClientCredentials
from coded_tools.tools.servicenow.auth import Token
from coded_tools.tools.servicenow.auth import TokenCache
from coded_tools.tools.servicenow.auth import build_provider
from coded_tools.tools.servicenow.auth import resolve_extra_headers
from coded_tools.tools.servicenow.errors import AuthError
from coded_tools.tools.servicenow.profile import AuthConfig
from coded_tools.tools.servicenow.scrub import REDACTED
from coded_tools.tools.servicenow.scrub import scrub_mapping
from coded_tools.tools.servicenow.scrub import scrub_text

from tests.coded_tools.tools.servicenow._test_base import sample_document
from tests.coded_tools.tools.servicenow._test_base import sample_profile

CLIENT_ID_ENV: str = "SN_TEST_CLIENT_ID"
CLIENT_SECRET_ENV: str = "SN_TEST_CLIENT_SECRET"
CREDENTIAL_ENV: str = "SN_TEST_GW_CREDENTIAL"


class FakeResponse:
    """Minimal stand-in for a requests Response."""

    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        """:return: The payload. :raises ValueError: when there is none."""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class AuthTestCase(TestCase):
    """Shared setup: synthetic credentials in the environment."""

    def setUp(self) -> None:
        self._saved: Dict[str, Optional[str]] = {}
        for name, value in ((CLIENT_ID_ENV, "test-client-id"),
                            (CLIENT_SECRET_ENV, "test-client-secret"),
                            (CREDENTIAL_ENV, "cHJlLWVuY29kZWQtdGVzdA==")):
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        self.addCleanup(self._restore)
        self.calls: list = []

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def auth_config(self, **overrides: Any) -> AuthConfig:
        """
        :param overrides: Fields to override.
        :return: An AuthConfig pointing at the synthetic env var names.
        """
        base: Dict[str, Any] = {
            "style": "standard",
            "token_url": "https://gateway.test/oauth/token",
            "client_id_env": CLIENT_ID_ENV,
            "client_secret_env": CLIENT_SECRET_ENV,
            "preencoded_credential_env": CREDENTIAL_ENV,
        }
        base.update(overrides)
        return AuthConfig(**base)

    def capture(self, response: FakeResponse):
        """
        :param response: The response every call should return.
        :return: A callable recording each request.
        """
        def _post(url: str, headers: Mapping[str, str] = None, timeout: Tuple = None,
                  verify: bool = True, **kwargs: Any) -> FakeResponse:
            self.calls.append({"url": url, "headers": dict(headers or {}),
                               "data": kwargs.get("data"), "json": kwargs.get("json"),
                               "verify": verify})
            return response
        return _post


class TestStandardFlow(AuthTestCase):
    """RFC 6749 client-credentials with Basic client authentication."""

    def test_sends_basic_from_id_and_secret_and_grant_type_only(self):
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"access_token": "abc", "expires_in": 60}))):
            token = provider.fetch_token()

        self.assertEqual(token.value, "abc")
        call = self.calls[0]
        expected = base64.b64encode(b"test-client-id:test-client-secret").decode()
        self.assertEqual(call["headers"]["Authorization"], f"Basic {expected}")
        self.assertEqual(call["data"], {"grant_type": "client_credentials"})
        self.assertNotIn("client_secret", call["data"])

    def test_percent_encoding_is_applied_by_default(self):
        os.environ[CLIENT_SECRET_ENV] = "secret with/special+chars"
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"access_token": "abc"}))):
            provider.fetch_token()
        decoded = base64.b64decode(
            self.calls[0]["headers"]["Authorization"].split(" ", 1)[1]).decode()
        self.assertIn("%2F", decoded)
        self.assertNotIn(" ", decoded)

    def test_percent_encoding_can_be_switched_off(self):
        os.environ[CLIENT_SECRET_ENV] = "secret with/special+chars"
        provider = StandardClientCredentials(
            self.auth_config(percent_encode_credentials=False), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"access_token": "abc"}))):
            provider.fetch_token()
        decoded = base64.b64decode(
            self.calls[0]["headers"]["Authorization"].split(" ", 1)[1]).decode()
        self.assertIn("secret with/special+chars", decoded)

    def test_scope_is_sent_when_configured(self):
        provider = StandardClientCredentials(
            self.auth_config(scope="records.read"), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"access_token": "abc"}))):
            provider.fetch_token()
        self.assertEqual(self.calls[0]["data"]["scope"], "records.read")


class TestPreEncodedFlow(AuthTestCase):
    """The variant flow, reproduced exactly rather than approximated."""

    def test_sends_opaque_credential_and_repeats_secret_in_body(self):
        provider = PreEncodedBasic(
            self.auth_config(style="preencoded", secret_body_key="client_security",
                             send_id_in_body=True), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"access_token": "abc"}))):
            provider.fetch_token()

        call = self.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Basic cHJlLWVuY29kZWQtdGVzdA==")
        self.assertEqual(call["data"]["client_id"], "test-client-id")
        # The body key is configuration, not an assumption compiled into the package.
        self.assertEqual(call["data"]["client_security"], "test-client-secret")

    def test_falls_back_to_json_on_unsupported_media_type(self):
        responses = [FakeResponse(415, text="unsupported"),
                     FakeResponse(200, {"access_token": "abc"})]

        def _post(url: str, headers: Mapping[str, str] = None, timeout: Tuple = None,
                  verify: bool = True, **kwargs: Any) -> FakeResponse:
            self.calls.append({"headers": dict(headers or {}), "data": kwargs.get("data"),
                               "json": kwargs.get("json")})
            return responses.pop(0)

        provider = PreEncodedBasic(self.auth_config(style="preencoded"), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post", _post):
            self.assertEqual(provider.fetch_token().value, "abc")

        self.assertEqual(self.calls[0]["headers"]["Content-Type"],
                         "application/x-www-form-urlencoded")
        self.assertEqual(self.calls[1]["headers"]["Content-Type"], "application/json")
        self.assertIsNotNone(self.calls[1]["json"])


class TestTokenFailures(AuthTestCase):
    """Failures must be explicit and must never echo credential material."""

    def test_missing_environment_variable_names_it_without_echoing_values(self):
        os.environ.pop(CLIENT_SECRET_ENV, None)
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with self.assertRaises(AuthError) as caught:
            provider.fetch_token()
        self.assertIn(CLIENT_SECRET_ENV, str(caught.exception))

    def test_rejection_body_is_scrubbed_before_it_reaches_the_message(self):
        # scrub-allow: synthetic token, the input this test exists to redact
        leaky = FakeResponse(401, text="denied for Bearer abcdef0123456789abcdef0123456789")
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post", self.capture(leaky)):
            with self.assertRaises(AuthError) as caught:
                provider.fetch_token()
        message = str(caught.exception)
        self.assertNotIn("abcdef0123456789", message)
        self.assertIn(REDACTED, message)

    def test_transient_connection_failure_is_retried(self):
        # A mint is idempotent. Without this retry a dropped connection fails the
        # whole tool call, while the identical drop on a business call is retried —
        # an asymmetry a flaky stub-server run exposed.
        import requests as requests_module  # pylint: disable=import-outside-toplevel
        attempts: Dict[str, int] = {"count": 0}

        def _post(*args: Any, **kwargs: Any) -> FakeResponse:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise requests_module.ConnectionError("connection reset")
            return FakeResponse(200, {"access_token": "abc"})

        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post", _post):
            with patch("coded_tools.tools.servicenow.auth.time.sleep", lambda _: None):
                self.assertEqual(provider.fetch_token().value, "abc")
        self.assertEqual(attempts["count"], 2)

    def test_persistent_connection_failure_gives_up_with_a_clear_reason(self):
        import requests as requests_module  # pylint: disable=import-outside-toplevel

        def _post(*args: Any, **kwargs: Any) -> FakeResponse:
            raise requests_module.ConnectionError("connection reset")

        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post", _post):
            with patch("coded_tools.tools.servicenow.auth.time.sleep", lambda _: None):
                with self.assertRaises(AuthError) as caught:
                    provider.fetch_token()
        self.assertEqual(caught.exception.reason, "auth_unreachable")

    def test_a_rejection_is_never_retried(self):
        # Repeating bad credentials cannot succeed and can lock an account.
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(401, text="denied"))):
            with self.assertRaises(AuthError):
                provider.fetch_token()
        self.assertEqual(len(self.calls), 1)

    def test_response_without_a_token_is_an_error_not_a_silent_none(self):
        provider = StandardClientCredentials(self.auth_config(), True, (5.0, 30.0))
        with patch("coded_tools.tools.servicenow.auth.requests.post",
                   self.capture(FakeResponse(200, {"token_type": "Bearer"}))):
            with self.assertRaises(AuthError) as caught:
                provider.fetch_token()
        self.assertEqual(caught.exception.reason, "auth_malformed")


class TestTokenCache(TestCase):
    """A warm token is reused; a stale one is replaced."""

    class CountingProvider:
        """Counts mints without touching a network."""

        def __init__(self, ttl: float = 3600.0):
            self.count = 0
            self.ttl = ttl
            self.style = "standard"

        def fetch_token(self) -> Token:
            """:return: A fresh synthetic token."""
            self.count += 1
            return Token(value=f"token-{self.count}", expires_at=time.time() + self.ttl)

    def test_warm_token_is_reused(self):
        cache = TokenCache()
        provider = self.CountingProvider()
        first, first_action, _ = cache.get(provider)
        second, second_action, _ = cache.get(provider)
        self.assertEqual(first, second)
        self.assertEqual(provider.count, 1)
        self.assertEqual((first_action, second_action), ("minted", "reused"))

    def test_token_inside_the_refresh_skew_is_replaced(self):
        # A token expiring in 5s is treated as stale: the 60s skew exists so a slow
        # call cannot race the boundary.
        cache = TokenCache()
        provider = self.CountingProvider(ttl=5.0)
        cache.get(provider)
        _, action, _ = cache.get(provider)
        self.assertEqual(action, "refreshed")
        self.assertEqual(provider.count, 2)

    def test_invalidate_forces_a_mint(self):
        cache = TokenCache()
        provider = self.CountingProvider()
        cache.get(provider)
        cache.invalidate()
        cache.get(provider)
        self.assertEqual(provider.count, 2)

    def test_provider_is_selected_by_profile_style(self):
        self.assertIsInstance(build_provider(sample_profile()), StandardClientCredentials)
        document = sample_document()
        document["auth"]["style"] = "preencoded"
        self.assertIsInstance(build_provider(sample_profile(auth=document["auth"])),
                              PreEncodedBasic)


class TestExtraHeaders(AuthTestCase):
    """Gateway-specific headers come from configuration, never from code."""

    def test_literal_and_environment_values_both_resolve(self):
        os.environ["SN_TEST_HEADER_VALUE"] = "resolved-from-env"
        self.addCleanup(lambda: os.environ.pop("SN_TEST_HEADER_VALUE", None))
        headers = resolve_extra_headers(self.auth_config(extra_headers={
            "X-Literal": "fixed-value",
            "X-FromEnv": "env:SN_TEST_HEADER_VALUE",
        }))
        self.assertEqual(headers["X-Literal"], "fixed-value")
        self.assertEqual(headers["X-FromEnv"], "resolved-from-env")

    def test_missing_environment_backed_header_fails_loudly(self):
        with self.assertRaises(AuthError):
            resolve_extra_headers(self.auth_config(
                extra_headers={"X-Missing": "env:SN_TEST_ABSENT_VALUE"}))


class TestScrubbing(TestCase):
    """Whatever an upstream echoes back, none of it reaches a log or the model."""

    def test_bearer_and_basic_headers_are_removed(self):
        self.assertNotIn("abcdef0123456789abcdef01",
                         scrub_text("Authorization: Bearer abcdef0123456789abcdef01"))
        self.assertNotIn("dXNlcjpwYXNz", scrub_text("Basic dXNlcjpwYXNzd29yZG9mbGVuZ3Ro"))

    def test_key_value_secrets_are_removed(self):
        scrubbed = scrub_text('{"client_secret": "hunter2", "state": "open"}')
        self.assertNotIn("hunter2", scrubbed)
        self.assertIn("state", scrubbed)

    def test_urls_hosts_and_addresses_are_removed(self):
        # scrub-allow: synthetic host and address, the input under redaction
        scrubbed = scrub_text("failed calling https://gw.example.com/api at 10.1.2.3")
        self.assertNotIn("gw.example.com", scrubbed)
        self.assertNotIn("10.1.2", scrubbed)

    def test_uuids_are_removed(self):
        # scrub-allow: synthetic UUID, the input under redaction
        text = "client 123e4567-e89b-12d3-a456-426614174000 rejected"
        self.assertNotIn("123e4567", scrub_text(text))

    def test_truncation_happens_after_redaction(self):
        # Truncating first could sever a token and leave a fragment that no longer
        # matches its pattern.
        # scrub-allow: synthetic token, the input under redaction
        text = ("padding " * 100) + "Bearer abcdef0123456789abcdef0123456789"
        scrubbed = scrub_text(text, limit=80)
        self.assertNotIn("abcdef0123456789", scrubbed)
        self.assertLessEqual(len(scrubbed), 81)

    def test_ordinary_text_survives(self):
        self.assertEqual(scrub_text("Record state is open"), "Record state is open")

    def test_mapping_leaves_are_scrubbed_and_shape_preserved(self):
        scrubbed: Dict[str, Any] = scrub_mapping({
            "status": 401,
            # scrub-allow: synthetic token and host, the input under redaction
            "detail": "Bearer abcdef0123456789abcdef0123456789",
            "nested": {"host": "https://gw.example.com/x"},
        })
        self.assertEqual(scrubbed["status"], 401)
        self.assertNotIn("abcdef0123456789", scrubbed["detail"])
        self.assertNotIn("gw.example.com", scrubbed["nested"]["host"])
