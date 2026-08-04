"""
The approval gate, tested adversarially.

Every test here asks the same question a reviewer will: can a model that is
confused, jailbroken, or following instructions injected into record content get a
write past this? The gate is only worth having if the answer is no for each of the
ways it could be attacked.
"""

import os
import time
from typing import Any
from typing import Dict
from unittest import TestCase

from coded_tools.tools.servicenow import gate
from coded_tools.tools.servicenow.context import SLY_CONSUMED_KEY
from coded_tools.tools.servicenow.errors import GateError

from tests.coded_tools.tools.servicenow._test_base import GATE_KEY
from tests.coded_tools.tools.servicenow._test_base import GATE_KEY_ENV
from tests.coded_tools.tools.servicenow._test_base import RECORD_ONE
from tests.coded_tools.tools.servicenow._test_base import RECORD_TWO
from tests.coded_tools.tools.servicenow._test_base import ROTATED_KEY
from tests.coded_tools.tools.servicenow._test_base import sample_document
from tests.coded_tools.tools.servicenow._test_base import sample_profile

CORRELATION: str = "correlation-under-test"


class GateTestCase(TestCase):
    """Shared setup: a signing key present in the environment."""

    def setUp(self) -> None:
        self._saved = os.environ.get(GATE_KEY_ENV)
        os.environ[GATE_KEY_ENV] = GATE_KEY
        self.addCleanup(self._restore)
        self.profile = sample_profile()
        self.fields: Dict[str, Any] = {"work_notes": "Access provisioned."}

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop(GATE_KEY_ENV, None)
        else:
            os.environ[GATE_KEY_ENV] = self._saved

    def mint(self, record: str = RECORD_ONE, fields: Dict[str, Any] = None):
        """
        :param record: Record the approval is for.
        :param fields: Fields the approval covers.
        :return: The minted token.
        """
        token, _ = gate.mint(self.profile, "request", record,
                             fields if fields is not None else self.fields, CORRELATION)
        return token


class TestHappyPath(GateTestCase):
    """An approval for exactly the change presented is accepted."""

    def test_matching_change_is_approved(self):
        verdict = gate.verify(self.profile, self.mint(), "request", RECORD_ONE, self.fields, {})
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.result, "approved")

    def test_correlation_id_survives_into_the_commit_turn(self):
        # Propose and commit are separate requests with separate sly_data, so the
        # id has to travel inside the token or the trail breaks in two.
        verdict = gate.verify(self.profile, self.mint(), "request", RECORD_ONE, self.fields, {})
        self.assertEqual(verdict.correlation_id, CORRELATION)

    def test_field_order_does_not_change_the_hash(self):
        token, _ = gate.mint(self.profile, "request", RECORD_ONE,
                             {"work_notes": "a", "state": "2"}, CORRELATION)
        verdict = gate.verify(self.profile, token, "request", RECORD_ONE,
                              {"state": "2", "work_notes": "a"}, {})
        self.assertTrue(verdict.ok)


class TestTampering(GateTestCase):
    """The attacks the gate exists to stop."""

    def test_no_token_is_refused(self):
        verdict = gate.verify(self.profile, None, "request", RECORD_ONE, self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "missing_token")

    def test_altered_values_after_approval_are_refused(self):
        token = self.mint()
        verdict = gate.verify(self.profile, token, "request", RECORD_ONE,
                              {"work_notes": "Access granted to everything."}, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "payload_mismatch")

    def test_extra_field_smuggled_after_approval_is_refused(self):
        token = self.mint()
        verdict = gate.verify(self.profile, token, "request", RECORD_ONE,
                              {**self.fields, "state": "7"}, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "payload_mismatch")

    def test_approval_cannot_be_reaimed_at_another_record(self):
        token = self.mint(record=RECORD_ONE)
        verdict = gate.verify(self.profile, token, "request", RECORD_TWO, self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "record_mismatch")

    def test_approval_cannot_be_reaimed_at_another_entity(self):
        token = self.mint()
        verdict = gate.verify(self.profile, token, "readonly", RECORD_ONE, self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "record_mismatch")

    def test_forged_token_fails_the_signature_check(self):
        # A model that guesses the token structure still cannot sign one.
        import base64  # pylint: disable=import-outside-toplevel
        import json  # pylint: disable=import-outside-toplevel
        claims = {"v": "v1", "h": gate.payload_hash("request", RECORD_ONE, self.fields),
                  "r": RECORD_ONE, "e": "request", "c": CORRELATION,
                  "x": int(time.time() + 300), "n": "forged"}
        body = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        verdict = gate.verify(self.profile, f"{body}.notarealsignature",
                              "request", RECORD_ONE, self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "bad_signature")

    def test_garbage_token_is_refused_without_raising(self):
        verdict = gate.verify(self.profile, "not-a-token", "request", RECORD_ONE,
                              self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "malformed_token")

    def test_expired_approval_is_refused(self):
        token, _ = gate.mint(self.profile, "request", RECORD_ONE, self.fields, CORRELATION,
                             now=time.time() - 10_000)
        verdict = gate.verify(self.profile, token, "request", RECORD_ONE, self.fields, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "expired")

    def test_replay_within_a_request_is_refused(self):
        sly_data: Dict[str, Any] = {}
        token = self.mint()
        self.assertTrue(gate.verify(self.profile, token, "request", RECORD_ONE,
                                    self.fields, sly_data).ok)
        self.assertIn(SLY_CONSUMED_KEY, sly_data)
        second = gate.verify(self.profile, token, "request", RECORD_ONE, self.fields, sly_data)
        self.assertFalse(second.ok)
        self.assertEqual(second.result, "already_consumed")


class TestKeyManagement(GateTestCase):
    """Rotation works; an unconfigured key disables gated writes rather than faking one."""

    def test_rotation_verifies_against_older_keys(self):
        token = self.mint()
        os.environ[GATE_KEY_ENV] = f"{ROTATED_KEY},{GATE_KEY}"
        verdict = gate.verify(self.profile, token, "request", RECORD_ONE, self.fields, {})
        self.assertTrue(verdict.ok, "A token signed before rotation must still verify.")

    def test_token_signed_by_an_unrelated_key_is_refused(self):
        token = self.mint()
        os.environ[GATE_KEY_ENV] = "a-completely-different-key"
        self.assertEqual(
            gate.verify(self.profile, token, "request", RECORD_ONE, self.fields, {}).result,
            "bad_signature")

    def test_missing_key_disables_the_gate_loudly(self):
        # The dangerous alternative — inventing a per-process key — works on one
        # replica and silently fails once a proposal and its commit land on
        # different pods. Refusing is the safe failure.
        os.environ.pop(GATE_KEY_ENV, None)
        with self.assertRaises(GateError) as caught:
            gate.verify(self.profile, "anything", "request", RECORD_ONE, self.fields, {})
        self.assertEqual(caught.exception.reason, "gate_unconfigured")


class TestAutoApprove(GateTestCase):
    """Opt-in, per deployment, and never the default."""

    def test_nothing_bypasses_the_gate_by_default(self):
        self.assertFalse(gate.is_auto_approved(self.profile, {"work_notes": "note"}))

    def test_opted_in_journal_field_bypasses(self):
        document = sample_document()
        document["gate"]["auto_approve_fields"] = ["work_notes"]
        profile = sample_profile(gate=document["gate"])
        verdict = gate.verify(profile, None, "request", RECORD_ONE, {"work_notes": "n"}, {})
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.result, "auto_approved")

    def test_opting_in_one_field_does_not_open_the_others(self):
        document = sample_document()
        document["gate"]["auto_approve_fields"] = ["work_notes"]
        profile = sample_profile(gate=document["gate"])
        verdict = gate.verify(profile, None, "request", RECORD_ONE,
                              {"work_notes": "n", "state": "3"}, {})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.result, "missing_token")
