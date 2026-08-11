"""
Profile loading and package-level routing.

The routing property under test: a registry names only a logical operation and
entity, and the package — not a HOCON file — turns that into a real endpoint.
"""

from typing import Any
from typing import Dict
from unittest import TestCase

from coded_tools.tools.servicenow.errors import PolicyDenied
from coded_tools.tools.servicenow.errors import ProfileError
from coded_tools.tools.servicenow.profile import _apply_env_overrides
from coded_tools.tools.servicenow.profile import build_profile
from coded_tools.tools.servicenow.router import Router
from coded_tools.tools.servicenow.router import Shape

from tests.coded_tools.tools.servicenow._test_base import sample_document
from tests.coded_tools.tools.servicenow._test_base import sample_profile


class TestProfileValidation(TestCase):
    """A misconfigured deployment must fail loudly, at startup, by key name."""

    def test_missing_base_url_names_the_key(self):
        document: Dict[str, Any] = sample_document()
        document.pop("base_url")
        with self.assertRaises(ProfileError) as caught:
            build_profile(document)
        self.assertIn("base_url", str(caught.exception))

    def test_unknown_auth_style_refused(self):
        document: Dict[str, Any] = sample_document()
        document["auth"]["style"] = "improvised"
        with self.assertRaises(ProfileError) as caught:
            build_profile(document)
        self.assertIn("auth.style", str(caught.exception))

    def test_entity_without_readable_fields_refused(self):
        document: Dict[str, Any] = sample_document()
        document["entities"]["request"]["read_fields"] = []
        with self.assertRaises(ProfileError):
            build_profile(document)

    def test_tls_verification_cannot_be_disabled(self):
        document: Dict[str, Any] = sample_document()
        document["verify_tls"] = False
        with self.assertRaises(ProfileError) as caught:
            build_profile(document)
        self.assertIn("verify_tls", str(caught.exception))

    def test_page_defaults_must_be_coherent(self):
        document: Dict[str, Any] = sample_document()
        document["limits"] = {"default_page": 50, "max_page": 10}
        with self.assertRaises(ProfileError):
            build_profile(document)

    def test_underscore_keys_are_treated_as_commentary(self):
        document: Dict[str, Any] = sample_document()
        document["_comment"] = ["human-readable note"]
        document["entities"]["_comment"] = "another note"
        profile = build_profile(document)
        self.assertNotIn("_comment", profile.entities)

    def test_env_overrides_reach_nested_keys(self):
        document: Dict[str, Any] = sample_document()
        environ = {
            "SN_PROFILE__operations__read__method": "HEAD",
            "SN_PROFILE__limits__max_page": "7",
        }
        merged = _apply_env_overrides(document, environ)
        profile = build_profile(merged)
        self.assertEqual(profile.operations["read"].method, "HEAD")
        self.assertEqual(profile.limits.max_page, 7)


class TestRouterValidation(TestCase):
    """The router cross-checks the profile against the shapes that exist in code."""

    def test_arbitrary_operation_names_are_allowed(self):
        # Operations are profile-defined; a new logical name of a known shape is
        # valid without any code change.
        document: Dict[str, Any] = sample_document()
        document["operations"]["read_related"] = {
            "method": "GET", "path": "/related/{table}", "shape": "query"}
        route = Router(build_profile(document)).resolve("read_related", "request")
        self.assertTrue(route.url.endswith("/related/sample_request_table"))

    def test_unknown_shape_is_refused(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["read"]["shape"] = "teleport"
        with self.assertRaises(ProfileError) as caught:
            build_profile(document)
        self.assertIn("shape", str(caught.exception))

    def test_fixed_path_without_table_resolves_as_non_entity_scoped(self):
        # A path with no {table} is a valid fixed endpoint, not an error.
        document: Dict[str, Any] = sample_document()
        document["operations"]["read"]["path"] = "/read/fixed"
        route = Router(build_profile(document)).resolve("read", "request")
        self.assertFalse(route.spec.entity_scoped)
        self.assertTrue(route.url.endswith("/read/fixed"))
        self.assertNotIn("sample_request_table", route.url)

    def test_auto_approve_field_no_entity_can_write_is_refused(self):
        document: Dict[str, Any] = sample_document()
        document["gate"]["auto_approve_fields"] = ["not_a_writable_field"]
        with self.assertRaises(ProfileError):
            Router(build_profile(document))


class TestRouting(TestCase):
    """Logical names in, bound routes out."""

    def setUp(self) -> None:
        self.router = Router(sample_profile())

    def test_resolves_logical_pair_to_a_bound_route(self):
        route = self.router.resolve("read", "request")
        self.assertEqual(route.method, "GET")
        self.assertTrue(route.url.endswith("/read/sample_request_table"))
        self.assertIs(route.spec.shape, Shape.QUERY)
        self.assertTrue(route.spec.retryable)

    def test_writes_are_never_marked_retryable(self):
        # An asynchronous create returning no reference duplicates records under
        # blind retry, so this property is load-bearing, not stylistic.
        self.assertFalse(self.router.resolve("update", "request").spec.retryable)

    def test_route_key_is_logical_and_leaks_no_destination(self):
        route = self.router.resolve("read", "request")
        self.assertEqual(route.route_key, "read:request")
        self.assertNotIn("http", route.route_key)
        self.assertNotIn("sample_request_table", route.route_key)

    def test_read_only_entity_refuses_a_write_route(self):
        with self.assertRaises(PolicyDenied):
            self.router.resolve("update", "readonly")

    def test_disabled_operation_cannot_route(self):
        with self.assertRaises(ProfileError) as caught:
            self.router.resolve("create", "request")
        self.assertIn("disabled", str(caught.exception))

    def test_unknown_entity_is_refused(self):
        with self.assertRaises(ProfileError):
            self.router.resolve("read", "not_configured")

    def test_record_placeholder_selects_path_addressing(self):
        document: Dict[str, Any] = sample_document()
        document["operations"]["update"]["path"] = "/update/{table}/{record}"
        route = Router(build_profile(document)).resolve("update", "request")
        self.assertTrue(route.record_in_path)
        self.assertTrue(route.bind_record("rec-9").url.endswith("/rec-9"))

    def test_without_the_placeholder_the_identifier_stays_out_of_the_path(self):
        route = self.router.resolve("update", "request")
        self.assertFalse(route.record_in_path)
        self.assertEqual(route.bind_record("rec-9").url, route.url)
