"""
The registry file itself.

Class-resolution and HOCON errors are the kind that stay invisible until an agent
network is loaded in a cluster, which is the worst place to find them. These checks
cost milliseconds and move that discovery to the laptop.

They also enforce the property the whole routing design exists for: the registry is
deployment-agnostic. If someone adds a table name or a URL to it, this fails.
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
REGISTRY: Path = REPO_ROOT / "registries" / "tools" / "servicenow.hocon"
PACKAGE: str = "coded_tools.tools.servicenow"

# The framework resolves "class": "module.ClassName" relative to
# <AGENT_TOOL_PATH>/<registry-stem>/, so the registry stem and the package
# directory name must agree.
EXPECTED_PACKAGE_DIRECTORY: str = "servicenow"


def load_registry() -> Dict[str, Any]:
    """
    :return: The parsed registry document.
    """
    return ConfigFactory.parse_file(str(REGISTRY)).as_plain_ordered_dict()


class TestRegistryParses(TestCase):
    """It must parse, and its shape must match what the framework expects."""

    def setUp(self) -> None:
        self.registry: Dict[str, Any] = load_registry()
        self.tools: List[Dict[str, Any]] = self.registry["tools"]

    def test_registry_stem_matches_the_package_directory(self):
        # Get this wrong and every class reference fails to resolve at load time.
        self.assertEqual(REGISTRY.stem, EXPECTED_PACKAGE_DIRECTORY)
        self.assertTrue((REPO_ROOT / "coded_tools" / "tools" / EXPECTED_PACKAGE_DIRECTORY).is_dir())

    def test_front_man_takes_only_a_free_form_inquiry(self):
        front_man: Dict[str, Any] = self.tools[0]
        properties = front_man["function"]["parameters"]["properties"]
        self.assertEqual(list(properties), ["inquiry"])
        self.assertNotIn("class", front_man,
                         "A front man cannot itself be a coded tool.")

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

    def test_every_coded_tool_pins_its_operation_and_entity(self):
        # Pinning here is what stops the model from choosing which table to reach;
        # a tool agent without it would take the destination from the LLM.
        for tool in self.tools:
            if "class" not in tool:
                continue
            args: Dict[str, Any] = tool.get("args", {})
            self.assertIn("operation", args, f"{tool['name']} does not pin an operation.")
            self.assertIn("entity", args, f"{tool['name']} does not pin an entity.")

    def test_every_declared_parameter_type_is_resolvable_by_the_framework(self):
        """
        Guard against a whole-network load failure that is nearly invisible.

        neuro-san converts each parameter into a pydantic field using its own
        TYPE_LOOKUP, which maps "int"/"float" — *not* the JSON-Schema standard
        "integer"/"number". An unrecognised type resolves to None, pydantic cannot
        infer the field, and the entire network is skipped at startup with one terse
        log line. Asserting against the framework's own table means a wrong type is
        caught here rather than as an empty agent list in a running server.
        """
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


class TestRegistryIsDeploymentAgnostic(TestCase):
    """The registry must be portable between environments without editing."""

    def setUp(self) -> None:
        self.text: str = REGISTRY.read_text(encoding="utf-8")

    def test_registry_names_no_destination(self):
        # Everything deployment-specific belongs in the profile. The only URL that
        # may appear is the license header.
        stripped = self.text.replace("http://www.apache.org/licenses/LICENSE-2.0", "")
        stripped = stripped.replace("www.cognizant.com", "")
        for pattern, label in (
            (r"[a-z]+://", "a URL"),
            (r"\bsysparm_\w+", "a wire parameter name"),
            (r"\$\{", "an environment reference"),
        ):
            self.assertIsNone(re.search(pattern, stripped),
                              f"The registry contains {label}; it belongs in the profile.")


class TestFrontManSecurityPolicy(TestCase):
    """The allow blocks are the framework's own security policy and must be present."""

    def setUp(self) -> None:
        self.front_man: Dict[str, Any] = load_registry()["tools"][0]

    def test_commit_token_is_released_upstream(self):
        # Without this the approval never reaches the client and the gate cannot
        # complete. Default is that no sly_data leaves the network at all.
        released = self.front_man["allow"]["to_upstream"]["sly_data"]
        self.assertTrue(released.get("sn_commit_token"))

    def test_sly_data_schema_advertises_what_must_come_back(self):
        schema = self.front_man["function"]["sly_data_schema"]["properties"]
        self.assertIn("sn_commit_token", schema)

    def test_tracing_exposes_only_the_correlation_id(self):
        traced = self.front_man["allow"]["to_tracing"]["sly_data"]
        self.assertEqual({key for key, value in traced.items() if value},
                         {"sn_correlation_id"},
                         "Only the correlation id is safe to un-redact in traces.")
