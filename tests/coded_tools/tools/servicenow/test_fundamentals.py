"""
Regression tests for the fundamentals-review gaps (F1–F3).

These three came from asking mission-level questions — "can this express the
gateway that actually exists?", "are the intended logs actually usable?", "is the
client contract stated?" — not from code reading. Every one had survived a green
suite because the tests inherited the same assumptions the code did.

  F1  A single profile-level base_url could not express the real topology: the one
      field-proven create endpoint lives on a different host and API version than
      the reads.
  F2  The framework's log formatter wraps logger messages in its own envelope
      without escaping, so JSON audit lines made the composite unparseable — the
      SIEM would reject every line. Measured on the deployed server, not inferred.
  F3  Two contracts existed only in conversation: the progress leg needs
      chat_filter MAXIMAL, and two of the four wire shapes are declared but not
      built. Undocumented contracts rot; these are pinned to the README by test.
"""

import json
import logging
import urllib.parse
import uuid
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from unittest import TestCase

from coded_tools.tools.servicenow.context import ToolContext
from coded_tools.tools.servicenow.errors import ProfileError
from coded_tools.tools.servicenow.profile import build_profile
from coded_tools.tools.servicenow.reporting import audit_line
from coded_tools.tools.servicenow.reporting import build_event
from coded_tools.tools.servicenow.router import Router

from tests.coded_tools.tools.servicenow._test_base import sample_document

REPO_ROOT: Path = Path(__file__).resolve().parents[4]


class TestF1PerOperationBase(TestCase):
    """One gateway, more than one host — expressible, validated, routed."""

    def test_operation_base_overrides_the_profile_base(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["create"] = {
            "method": "POST",
            "path": "/make/{table}",
            "base_url": "https://elsewhere.test/api/v2",
            "enabled": True,
        }
        route = Router(build_profile(document)).resolve("create", "request")
        self.assertTrue(route.url.startswith("https://elsewhere.test/api/v2/"),
                        "The operation-level base must win.")

    def test_operations_without_an_override_keep_the_shared_base(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["create"] = {
            "method": "POST", "path": "/make/{table}",
            "base_url": "https://elsewhere.test/api/v2", "enabled": True,
        }
        router = Router(build_profile(document))
        self.assertTrue(router.resolve("read", "request").url
                        .startswith("https://gateway.test/api/"),
                        "An override on one operation must not leak to the others.")

    def test_a_blank_override_is_refused_by_name(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["read"]["base_url"] = "   "
        with self.assertRaises(ProfileError) as caught:
            build_profile(document)
        self.assertIn("base_url", str(caught.exception))

    def test_trailing_slash_on_the_override_does_not_double_up(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["read"]["base_url"] = "https://elsewhere.test/api/"
        route = Router(build_profile(document)).resolve("read", "request")
        self.assertNotIn("//read", route.url)


class TestF2EnvelopeSafeAuditLines(TestCase):
    """The audit line must survive any naive wrapper and still decode."""

    def event(self) -> Dict[str, Any]:
        """:return: A representative scrubbed event."""
        context = ToolContext(correlation_id=uuid.uuid4().hex, origin="Net/Tool",
                              tool="T", operation="update", entity="request")
        return build_event(context, {
            "servicenow_change_verified": True,
            "record": "rc-1",
            "reason": 'the change presented differs from the change approved',
            "fields": ["state", "work_notes"],
            "nested": {"detail": "value with spaces"},
        })

    def test_line_contains_nothing_that_can_break_an_envelope(self):
        line: str = audit_line(self.event())
        for forbidden in ('"', "\\", "{", "}", "\n", "\r"):
            self.assertNotIn(forbidden, line,
                             f"{forbidden!r} would break out of a wrapping string.")

    def test_line_survives_the_framework_style_naive_envelope(self):
        # This is the exact failure measured on the deployed server: the framework
        # interpolates the message into its own JSON envelope without escaping.
        line: str = audit_line(self.event())
        envelope: str = ('{"message": "' + line + '", "user_id": "None", '
                         '"source": "HttpServer", "request_id": "request-1"}')
        parsed = json.loads(envelope)
        self.assertEqual(parsed["message"], line)
        self.assertEqual(parsed["request_id"], "request-1")

    def test_line_is_mechanically_decodable(self):
        event: Dict[str, Any] = self.event()
        decoded: Dict[str, str] = {}
        for pair in audit_line(event).split(" "):
            key, _, value = pair.partition("=")
            decoded[key] = urllib.parse.unquote(value)
        self.assertEqual(decoded["correlation_id"], event["correlation_id"])
        self.assertEqual(decoded["record"], "rc-1")
        self.assertEqual(decoded["nested.detail"], "value with spaces")
        self.assertEqual(decoded["servicenow_change_verified"], "true")
        self.assertEqual(decoded["fields"], "state,work_notes")

    def test_the_correlation_id_appears_raw_and_greppable(self):
        # Hex needs no encoding, so an investigator can grep the id verbatim.
        event: Dict[str, Any] = self.event()
        self.assertIn(f"correlation_id={event['correlation_id']}", audit_line(event))

    def test_the_durable_leg_emits_the_safe_line(self):
        captured: List[str] = []

        class Capture(logging.Handler):
            """Collects emitted messages."""
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        import asyncio  # pylint: disable=import-outside-toplevel
        from coded_tools.tools.servicenow.reporting import report  # pylint: disable=import-outside-toplevel
        logger = logging.getLogger("F2/Origin")
        handler = Capture()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)

        context = ToolContext(correlation_id="cid-1", origin="F2/Origin", tool="T",
                              operation="read", entity="request")
        asyncio.run(report({"origin_str": "F2/Origin"}, context,
                           {"servicenow_downstream_call": True, "status": 200}))
        self.assertEqual(len(captured), 1)
        self.assertNotIn('"', captured[0])
        self.assertIn("servicenow_downstream_call=true", captured[0])


class TestF3ContractsAreWrittenDown(TestCase):
    """Contracts that live only in conversation rot; pin them to the README."""

    def readme(self) -> str:
        """:return: The README text."""
        return (REPO_ROOT / "coded_tools" / "tools" / "servicenow" / "README.md").read_text(encoding="utf-8")

    def test_progress_leg_contract_is_documented(self):
        text: str = self.readme()
        self.assertIn("MAXIMAL", text,
                      "The README must state that progress markers require the "
                      "client to request chat_filter MAXIMAL.")

    def test_unbuilt_shapes_are_documented(self):
        # Whitespace-normalised so a reflow of the paragraph does not break the
        # guard — the contract is the sentence, not its line wrapping.
        text: str = " ".join(self.readme().replace("**", "").split())
        self.assertIn("declared but not built", text,
                      "The README must state that BINARY/MULTIPART are declared "
                      "but not built.")

    def test_audit_line_format_is_documented(self):
        self.assertIn("logfmt", self.readme(),
                      "The README must explain why the durable line is not JSON.")

    def test_per_operation_base_is_documented_in_the_example_profile(self):
        example: str = (REPO_ROOT / "coded_tools" / "tools" / "servicenow"
                        / "profile.example.json").read_text(
            encoding="utf-8")
        self.assertIn("base_url (optional, any operation)", example)
