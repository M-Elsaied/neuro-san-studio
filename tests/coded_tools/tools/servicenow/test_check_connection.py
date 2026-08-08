"""
The connection checker itself gets tested — it is the first thing a new
environment runs, so a checker that lies (passes when the path is broken, or
crashes instead of naming the problem) would poison every diagnosis after it.
"""

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict
from typing import Optional
from unittest import TestCase
from unittest.mock import patch

from coded_tools.tools.servicenow import profile as profile_module
from coded_tools.tools.servicenow import transport as transport_module
from coded_tools.tools.servicenow.check_connection import main
from coded_tools.tools.servicenow.demo.stub_gateway import StubGateway

RECORD = {"sys_id": "cc-0001", "number": "REQ0001",
          "short_description": "Connectivity sample", "state": "1"}


class TestCheckConnection(TestCase):
    """Runs the real checker in-process against the real stub over real HTTP."""

    def setUp(self) -> None:
        self._saved: Dict[str, Optional[str]] = {}
        self.stub = StubGateway(records={"cc-0001": dict(RECORD)})
        self.stub.__enter__()
        self.addCleanup(self._teardown)

        document = self.stub.profile_document()
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        json.dump(document, handle)
        handle.close()
        self._profile_path = handle.name

        for name, value in (("SN_PROFILE_FILE", self._profile_path),
                            ("SN_CLIENT_ID", "check-id"),
                            ("SN_CLIENT_SECRET", "check-secret")):
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value

        profile_module.set_profile(None)
        transport_module.reset_transport_state()

    def _teardown(self) -> None:
        self.stub.__exit__(None, None, None)
        Path(self._profile_path).unlink(missing_ok=True)
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        profile_module.set_profile(None)
        transport_module.reset_transport_state()

    def run_checker(self, *argv: str):
        """
        :param argv: CLI arguments.
        :return: (exit code, captured stdout)
        """
        buffer = io.StringIO()
        # Autoload is disabled here: a developer following the README keeps real
        # SN_* values in their repo .env, and letting the checker read it would
        # quietly satisfy the missing-credential scenarios these tests stage.
        with patch("coded_tools.tools.servicenow.check_connection."
                   "load_dotenv_if_present", return_value=None):
            with redirect_stdout(buffer):
                code = main(["--quiet", *argv])
        return code, buffer.getvalue()

    def test_healthy_path_passes_every_step(self):
        code, out = self.run_checker()
        self.assertEqual(code, 0, out)
        for step in ("profile", "credentials[standard]", "token[standard]",
                     "route", "read"):
            self.assertIn(f"[PASS] {step}", out)
        self.assertIn("correlation_id for this check:", out)

    def test_specific_record_lookup(self):
        code, out = self.run_checker("--record", "REQ0001")
        self.assertEqual(code, 0, out)
        self.assertIn("1 record(s)", out)

    def test_missing_record_fails_by_name(self):
        code, out = self.run_checker("--record", "REQ9999")
        self.assertEqual(code, 1)
        self.assertIn("no record matched", out)

    def test_missing_credential_named_never_printed(self):
        os.environ.pop("SN_CLIENT_SECRET", None)
        code, out = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("SN_CLIENT_SECRET", out,
                      "The failing variable must be named so the user knows "
                      "what to export.")
        # Variable NAMES are named; credential VALUES never appear.
        self.assertNotIn("check-id", out)
        self.assertNotIn("check-secret", out)

    def test_missing_profile_fails_with_remedy(self):
        os.environ["SN_PROFILE_FILE"] = self._profile_path + ".does-not-exist"
        code, out = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] profile", out)
        self.assertIn("Remedy", out)

    def test_wrong_credentials_reported_as_rejection_not_crash(self):
        # The stub accepts any Basic header, so simulate rejection by pointing the
        # token URL somewhere that answers 404.
        document = json.loads(Path(self._profile_path).read_text(encoding="utf-8"))
        document["auth"]["token_url"] = self.stub.base_url + "/read/nothing"  # 401 path
        Path(self._profile_path).write_text(json.dumps(document), encoding="utf-8")
        code, out = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] token[standard]", out)

    def test_try_both_reports_which_flows_work(self):
        code, out = self.run_checker("--try-both")
        self.assertEqual(code, 0, out)
        self.assertIn("auth flows accepted by this gateway:", out)
        self.assertIn("standard", out.split("auth flows accepted")[-1])
        # The variant flow's extra credential is unset, so it must be reported as
        # skipped rather than crashing the whole check.
        self.assertIn("[----] token[preencoded]", out)

    def test_token_value_never_appears_in_output(self):
        # scrub-allow: the stub's fixed issued token, asserted ABSENT
        code, out = self.run_checker()
        self.assertEqual(code, 0)
        self.assertNotIn("stub-issued-token", out)


class TestDotenvAutoload(TestCase):
    """The loader follows launcher semantics: fill gaps, never override."""

    def test_fills_missing_and_never_overrides(self):
        from coded_tools.tools.servicenow.check_connection import load_dotenv_if_present
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                'SN_AUTOLOAD_A="from-file"\nSN_AUTOLOAD_B="from-file"\n',
                encoding="utf-8")
            saved = {k: os.environ.get(k) for k in ("SN_AUTOLOAD_A", "SN_AUTOLOAD_B")}
            os.environ["SN_AUTOLOAD_A"] = "from-shell"
            os.environ.pop("SN_AUTOLOAD_B", None)
            try:
                loaded = load_dotenv_if_present(env_file)
                self.assertEqual(loaded, str(env_file))
                self.assertEqual(os.environ["SN_AUTOLOAD_A"], "from-shell",
                                 "Shell values must always win.")
                self.assertEqual(os.environ["SN_AUTOLOAD_B"], "from-file",
                                 "Gaps must be filled from the file.")
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_absent_file_is_a_quiet_no_op(self):
        from coded_tools.tools.servicenow.check_connection import load_dotenv_if_present
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_dotenv_if_present(Path(folder) / ".env"))
