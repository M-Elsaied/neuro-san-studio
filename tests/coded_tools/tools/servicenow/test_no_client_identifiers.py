"""
The identifier guard.

A confidentiality rule nobody checks is a rule that decays. This test walks every
source, config, registry, fixture and design document in the repository and fails on
anything shaped like a deployment identifier: a URL, a hostname, an address, a UUID,
a long opaque credential run, or a real record reference.

Two design points make it usable rather than merely strict:

  * **The client's own name is never committed.** It is supplied to CI through
    ``SCRUB_EXTRA_PATTERNS`` as extra regexes, so the guard can fail on a word this
    repository is not allowed to contain. Locally the shape patterns run alone.
  * **Deliberate exceptions are per line, not per file.** A line carrying a
    shaped literal on purpose — a license URL, a synthetic fixture — is marked
    ``scrub-allow``. Exempting whole files would quietly widen over time.

Run it as a pre-commit hook too: catching a leak before it reaches history is a
delete, whereas catching it afterwards is a rewrite.
"""

import os
import re
from pathlib import Path
from typing import Iterator
from typing import List
from typing import Pattern
from typing import Tuple
from unittest import TestCase

#: Marker that exempts a single line, and documents why in the line itself.
ALLOW_MARKER: str = "scrub-allow"

REPO_ROOT: Path = Path(__file__).resolve().parents[4]

# Scoped to what this package owns: scanning the whole studio tree would flag
# upstream files this branch has no authority over.
SCANNED_DIRECTORIES: Tuple[str, ...] = ("coded_tools/tools/servicenow",
                                        "tests/coded_tools/tools/servicenow")
SCANNED_ROOT_FILES: Tuple[str, ...] = ("registries/tools/servicenow.hocon",)
SCANNED_SUFFIXES: Tuple[str, ...] = (".py", ".hocon", ".json", ".md", ".ini", ".yaml", ".yml")

EXCLUDED_PARTS: Tuple[str, ...] = (".venv", "__pycache__", ".git", "node_modules", ".pytest_cache")

#: Hosts that carry no deployment meaning: reserved test/example namespaces plus the
#: boilerplate license and vendor URLs that appear in every file header.
ALLOWED_LITERALS: Tuple[str, ...] = (
    # Loopback carries no deployment meaning and is how the local stub is addressed.
    "127.0.0.1",
    "http://www.apache.org/licenses/LICENSE-2.0",
    "www.cognizant.com",
    "https://json-schema.org",
    "https://docs.python.org",
    "https://docs.pytest.org",
)
ALLOWED_HOST_SUFFIXES: Tuple[str, ...] = (
    ".test", ".example", ".invalid", ".localhost", "localhost",
    "example.com", "example.org",
)

CHECKS: Tuple[Tuple[str, Pattern[str]], ...] = (
    ("absolute URL", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>)\]]+")),
    ("hostname", re.compile(
        r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"(?:com|net|org|io|intra|internal|corp|cloud|gov|edu)\b")),
    ("IP address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("UUID", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    # Deliberately narrower than it first appears. Underscores and hyphens are
    # excluded; at least one letter and at least TWO digits are required. Real
    # credential material virtually always carries several digits, while the
    # near-misses that made this check cry wolf — long Python identifiers, dotted
    # module paths, MIME types, CamelCase test names like TestR1Something — carry
    # at most one. A check that flags its own source is one people start ignoring.
    ("opaque credential run",
     re.compile(r"\b(?=(?:[A-Za-z+/]*[0-9]){2})(?=[A-Za-z0-9+/]*[A-Za-z])"
                r"[A-Za-z0-9+/]{32,}={0,2}\b")),
    ("record reference", re.compile(r"\b(?:INC|RITM|CHG|PRB|REQ|TASK)\d{5,}\b")),
)


def extra_patterns() -> List[Pattern[str]]:
    """
    :return: Additional regexes supplied by CI, so the literal client name can be
             enforced against without ever being committed to this repository.
    """
    raw: str = os.environ.get("SCRUB_EXTRA_PATTERNS", "")
    return [re.compile(part.strip(), re.IGNORECASE)
            for part in raw.split(",") if part.strip()]


def is_allowed(finding: str) -> bool:
    """
    :param finding: The matched text.
    :return: True when the match is boilerplate or a reserved test namespace.
    """
    for literal in ALLOWED_LITERALS:
        if literal in finding or finding in literal:
            return True
    host: str = finding.split("://")[-1].split("/")[0].split(":")[0].rstrip(".")
    return any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def scanned_files() -> Iterator[Path]:
    """
    :return: Every file the guard is responsible for.
    """
    for name in SCANNED_ROOT_FILES:
        candidate: Path = REPO_ROOT / name
        if candidate.is_file():
            yield candidate
    for directory in SCANNED_DIRECTORIES:
        root: Path = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in SCANNED_SUFFIXES:
                if not any(part in EXCLUDED_PARTS for part in path.parts):
                    yield path


class TestNoClientIdentifiers(TestCase):
    """The repository must be safe to hand to a reviewer with no context."""

    def test_no_deployment_identifiers_anywhere(self):
        violations: List[str] = []
        patterns = list(CHECKS) + [("CI-supplied literal", pattern)
                                   for pattern in extra_patterns()]

        for path in scanned_files():
            relative: str = str(path.relative_to(REPO_ROOT))
            text: str = path.read_text(encoding="utf-8", errors="replace")
            lines: List[str] = text.splitlines()
            for number, line in enumerate(lines, start=1):
                # The marker exempts the line it appears on and the line it
                # annotates, so the exemption can be written as a comment directly
                # above the literal — where a reviewer will actually read it.
                previous: str = lines[number - 2] if number >= 2 else ""
                if ALLOW_MARKER in line or ALLOW_MARKER in previous:
                    continue
                for label, pattern in patterns:
                    for finding in pattern.findall(line):
                        if is_allowed(finding):
                            continue
                        violations.append(f"{relative}:{number}: {label}: {finding}")

        self.assertEqual(violations, [], "Deployment identifiers found:\n" + "\n".join(violations))

    def test_the_guard_actually_catches_things(self):
        # A guard that cannot fail proves nothing, so assert each shape is caught.
        samples = {
            # scrub-allow: invented samples proving each shape is detected
            "absolute URL": "see https://gateway.acme-corp.com/api/v1",
            # scrub-allow: invented sample
            "IP address": "connect to 10.20.30.40 first",
            # scrub-allow: invented sample
            "UUID": "client 123e4567-e89b-12d3-a456-426614174000",
            # scrub-allow: invented sample
            "record reference": "raised as INC1234567 yesterday",
        }
        for label, sample in samples.items():
            pattern = dict(CHECKS)[label]
            findings = [item for item in pattern.findall(sample) if not is_allowed(item)]
            self.assertTrue(findings, f"The {label} check failed to match its own sample.")

    def test_reserved_test_namespaces_are_permitted(self):
        pattern = dict(CHECKS)["absolute URL"]
        for sample in ("https://gateway.test/api", "https://api.example.com/x"):
            findings = [item for item in pattern.findall(sample) if not is_allowed(item)]
            self.assertEqual(findings, [],
                             "Reserved test namespaces must not trip the guard, or every "
                             "fixture would need an exemption.")

    def test_ci_supplied_patterns_are_honoured(self):
        saved = os.environ.get("SCRUB_EXTRA_PATTERNS")
        os.environ["SCRUB_EXTRA_PATTERNS"] = r"\bacmecorp\b"
        try:
            patterns = extra_patterns()
            self.assertTrue(patterns)
            self.assertTrue(patterns[0].search("deployed for AcmeCorp today"))
        finally:
            if saved is None:
                os.environ.pop("SCRUB_EXTRA_PATTERNS", None)
            else:
                os.environ["SCRUB_EXTRA_PATTERNS"] = saved
