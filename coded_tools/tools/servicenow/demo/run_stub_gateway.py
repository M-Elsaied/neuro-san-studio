"""
Run the stub gateway on a fixed port and write a matching deployment profile.

This is what makes the whole thing demonstrable without any client access: the stub
stands in for the enterprise gateway, so the agent network, the coded tools, the
approval gate and the audit trail can all be exercised end to end on a laptop.

    PYTHONPATH=. python coded_tools/servicenow/demo/run_stub_gateway.py

Writes profile.local.json beside this file (gitignored) and serves until interrupted.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from typing import Dict

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
from coded_tools.tools.servicenow.demo.stub_gateway import StubGateway  # noqa: E402

DEFAULT_PORT: int = 8099
PROFILE_PATH: Path = Path(__file__).resolve().parent / "profile.local.json"

DEMO_RECORDS: Dict[str, Dict[str, Any]] = {
    "a1b2c3": {
        "sys_id": "a1b2c3",
        "number": "REQ0001",
        "short_description": "Access to the analytics workspace for a new starter",
        "state": "1",
        "priority": "3",
        "assigned_to": "A. Reviewer",
        "opened_at": "2026-08-01 09:14:00",
        "work_notes": "",
    },
    "d4e5f6": {
        "sys_id": "d4e5f6",
        "number": "REQ0002",
        "short_description": "Elevated reporting role, quarter-end close",
        "state": "2",
        "priority": "2",
        "assigned_to": "B. Approver",
        "opened_at": "2026-08-02 11:02:00",
        "work_notes": "",
    },
    "g7h8i9": {
        "sys_id": "g7h8i9",
        "number": "REQ0003",
        "short_description": "Read-only access to the finance dataset",
        "state": "1",
        "priority": "4",
        "assigned_to": "A. Reviewer",
        "opened_at": "2026-08-03 08:30:00",
        "work_notes": "",
    },
}


# What a real gateway returns when asked for display values. The stub carries this
# so it is no more forgiving than the system it stands in for — otherwise it hides
# exactly the failure it exists to surface.
DISPLAY_VALUES: Dict[str, Dict[str, str]] = {
    "state": {"1": "New", "2": "In Progress", "3": "Resolved", "4": "Closed"},
    "priority": {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low"},
}


def main() -> int:
    """
    :return: Process exit code.
    """
    port: int = int(os.environ.get("SN_STUB_PORT", DEFAULT_PORT))
    stub = StubGateway(records=DEMO_RECORDS, port=port, display_values=DISPLAY_VALUES)

    profile: Dict[str, Any] = stub.profile_document()
    # Widen the demo entity beyond the test fixture so the UI has something to show.
    profile["entities"]["request"] = {
        "table": "demo_request_table",
        "identifier_field": "sys_id",
        "display_field": "number",
        "read_fields": ["sys_id", "number", "short_description", "state",
                        "priority", "assigned_to", "opened_at"],
        "write_fields": ["work_notes", "state"],
        # Without these, the model is handed bare codes and invents meanings for
        # them — the same code was rendered "New" on one run and "Open" on the next.
        "coded_fields": {
            "state": {"1": "New", "2": "In Progress", "3": "Resolved", "4": "Closed"},
            "priority": {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low"},
        },
    }

    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    with stub:
        print(f"Stub gateway listening on {stub.base_url}")
        print(f"Profile written to {PROFILE_PATH}")
        print(f"Serving {len(DEMO_RECORDS)} synthetic records. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping stub gateway.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
