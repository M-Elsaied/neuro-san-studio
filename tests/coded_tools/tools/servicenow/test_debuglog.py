"""
The opt-in debug channel: silent by default, and when on it shows the URL stitch
without ever emitting a secret.

This exists because the durable audit stream omits the host (a deployment
identifier), which is what made URL stitching hard to debug. The debug channel is
the sanctioned way to see it locally.
"""

import logging
from typing import List
from unittest import TestCase

from coded_tools.tools.servicenow import debuglog
from coded_tools.tools.servicenow.router import Router

from tests.coded_tools.tools.servicenow._test_base import sample_profile


class _Capture(logging.Handler):
    """Collects emitted messages for assertion."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class DebugLogTestCase(TestCase):
    """Snapshot and restore the package logger so tests don't leak global state."""

    def setUp(self) -> None:
        self.logger = logging.getLogger(debuglog.PACKAGE_LOGGER)
        self._saved_level = self.logger.level
        self._saved_propagate = self.logger.propagate
        self._saved_handlers = list(self.logger.handlers)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.logger.setLevel(self._saved_level)
        self.logger.propagate = self._saved_propagate
        self.logger.handlers = self._saved_handlers


class TestEnableSwitch(DebugLogTestCase):
    """SN_DEBUG controls activation; enabling is idempotent."""

    def test_respects_sn_debug_env(self):
        import os  # pylint: disable=import-outside-toplevel
        saved = os.environ.get("SN_DEBUG")
        try:
            os.environ.pop("SN_DEBUG", None)
            self.assertFalse(debuglog.enable_debug_if_requested())
            os.environ["SN_DEBUG"] = "1"
            self.assertTrue(debuglog.enable_debug_if_requested())
        finally:
            if saved is None:
                os.environ.pop("SN_DEBUG", None)
            else:
                os.environ["SN_DEBUG"] = saved

    def test_enable_is_idempotent(self):
        debuglog.enable_debug()
        count = sum(getattr(h, "_sn_debug", False) for h in self.logger.handlers)
        debuglog.enable_debug()
        count_again = sum(getattr(h, "_sn_debug", False) for h in self.logger.handlers)
        self.assertEqual(count, 1)
        self.assertEqual(count_again, 1)


class TestStitchTrace(DebugLogTestCase):
    """When debug is on, the stitch line names every piece of the URL."""

    def test_stitch_line_shows_the_composition(self):
        capture = _Capture()
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(capture)

        Router(sample_profile()).resolve("read", "request")

        stitch = [m for m in capture.messages if "stitch read/request" in m]
        self.assertTrue(stitch, "a stitch line should be emitted at DEBUG")
        line = stitch[0]
        self.assertIn("base=", line)
        self.assertIn("path=", line)
        self.assertIn("sample_request_table", line)   # the table substitution
        self.assertIn("query_params=", line)

    def test_silent_when_debug_off(self):
        capture = _Capture()
        self.logger.setLevel(logging.WARNING)   # debug suppressed
        self.logger.addHandler(capture)
        Router(sample_profile()).resolve("read", "request")
        self.assertEqual(capture.messages, [],
                         "nothing should be emitted below DEBUG level")
