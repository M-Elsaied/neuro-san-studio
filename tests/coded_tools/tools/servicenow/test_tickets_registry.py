"""
The read-only ticket-lookup registry (servicenow_tickets.hocon).

Same class of invisible-until-cluster failures as test_registry.py, applied to the
simple read-only network: it must parse, resolve its classes, stay
deployment-agnostic, pin operation+entity on every reader, use only framework-
resolvable parameter types, and never let the model pick a table or operation.
"""

import importlib
import re
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from unittest import TestCase

from pyhocon import ConfigFactory

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
REGISTRY: Path = REPO_ROOT / "registries" / "tools" / "servicenow_tickets.hocon"
PACKAGE: str = "coded_tools.tools.servicenow"


def load_registry() -> Dict[str, Any]:
    """:return: The parsed registry document."""
    return ConfigFactory.parse_file(str(REGISTRY)).as_plain_ordered_dict()


class TestTicketsRegistryParses(TestCase):
    """It must parse and match the shape the framework expects."""

    def setUp(self) -> None:
        self.registry: Dict[str, Any] = load_registry()
        self.tools: List[Dict[str, Any]] = self.registry["tools"]

    def test_front_man_takes_only_a_free_form_inquiry(self):
        front_man: Dict[str, Any] = self.tools[0]
        properties = front_man["function"]["parameters"]["properties"]
        self.assertEqual(list(properties), ["inquiry"])
        self.assertNotIn("class", front_man,
                         "A front man cannot itself be a coded tool.")

    def test_it_is_read_only(self):
        # The whole point of this network is a safe read demo: no write tool may
        # appear, so it can never mutate a record even by misconfiguration.
        for tool in self.tools:
            args: Dict[str, Any] = tool.get("args", {})
            if "operation" in args:
                self.assertEqual(args["operation"], "read",
                                 f"{tool['name']} is not a read; this network is read-only.")

    def test_every_referenced_tool_agent_exists(self):
        names = {tool["name"] for tool in self.tools}
        for tool in self.tools:
            for referenced in tool.get("tools", []):
                self.assertIn(referenced, names,
                              f"{tool['name']} references a tool that is not defined.")

    def test_every_class_reference_resolves(self):
        for tool in self.tools:
            reference: str = tool.get("class", "")
            if not reference:
                continue
            module_name, _, class_name = reference.rpartition(".")
            module = importlib.import_module(f"{PACKAGE}.{module_name}")
            self.assertTrue(hasattr(module, class_name),
                            f"{reference} does not name a class that exists.")

    def test_every_reader_pins_its_operation_and_entity(self):
        for tool in self.tools:
            if "class" not in tool:
                continue
            args: Dict[str, Any] = tool.get("args", {})
            self.assertIn("operation", args, f"{tool['name']} does not pin an operation.")
            self.assertIn("entity", args, f"{tool['name']} does not pin an entity.")

    def test_every_declared_parameter_type_is_resolvable_by_the_framework(self):
        # pylint: disable=import-outside-toplevel
        from neuro_san.internals.run_context.langchain.core.base_model_dictionary_converter \
            import BaseModelDictionaryConverter

        known = set(BaseModelDictionaryConverter.TYPE_LOOKUP) | {"object", "array"}
        for tool in self.tools:
            properties: Dict[str, Any] = \
                tool["function"]["parameters"].get("properties", {})
            for field, spec in properties.items():
                self.assertIn(
                    spec.get("type"), known,
                    f"{tool['name']}.{field} declares type '{spec.get('type')}', which "
                    f"this neuro-san version cannot resolve. Known: {sorted(known)}.")

    def test_no_tool_exposes_table_or_operation_as_an_llm_parameter(self):
        for tool in self.tools:
            if "class" not in tool:
                continue
            properties = tool["function"]["parameters"].get("properties", {})
            for forbidden in ("table", "entity", "operation", "url", "path"):
                self.assertNotIn(forbidden, properties,
                                 f"{tool['name']} lets the model choose '{forbidden}'.")


class TestTicketsRegistryIsDeploymentAgnostic(TestCase):
    """No host, table, wire-parameter or environment reference may appear."""

    def setUp(self) -> None:
        self.text: str = REGISTRY.read_text(encoding="utf-8")

    def test_registry_names_no_destination(self):
        stripped = self.text.replace("http://www.apache.org/licenses/LICENSE-2.0", "")
        stripped = stripped.replace("www.cognizant.com", "")
        for pattern, label in (
            (r"[a-z]+://", "a URL"),
            (r"\bsysparm_\w+", "a wire parameter name"),
            (r"\$\{", "an environment reference"),
        ):
            self.assertIsNone(re.search(pattern, stripped),
                              f"The registry contains {label}; it belongs in the profile.")
