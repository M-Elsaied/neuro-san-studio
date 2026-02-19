# Copyright © 2025-2026 Cognizant Technology Solutions Corp, www.cognizant.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# END COPYRIGHT

from typing import Any
from typing import Dict

from neuro_san.interfaces.coded_tool import CodedTool


# Documented failure modes for PVC injection moulding.
# Chemical migration / plasticiser leaching from thermal degradation is
# NOT included — the FMEA scope note redirects that concern to material
# certification.
_FAILURE_MODES = {
    "pvc_injection_moulding": {
        "process_type": "PVC Injection Moulding",
        "fmea_reference": "FMEA-PVC-IM-2023-R4",
        "failure_modes": [
            {
                "id": "FM-001",
                "failure_mode": "Warping / Distortion",
                "cause": "Uneven cooling or excessive mould temperature differential",
                "detection_method": "Visual inspection and dimensional gauging",
                "risk_parameters": {
                    "barrel_temp_risk": "LOW if within 180-200°C range",
                    "cooling_time_risk": "MEDIUM if below 15s",
                },
                "mitigation": "Maintain uniform mould temperature; ensure adequate cooling time",
            },
            {
                "id": "FM-002",
                "failure_mode": "Sink Marks",
                "cause": "Insufficient packing pressure or premature gate freeze-off",
                "detection_method": "Visual inspection under angled lighting",
                "risk_parameters": {
                    "injection_pressure_risk": "LOW if within 80-120 bar",
                    "barrel_temp_risk": "LOW if within 180-200°C range",
                },
                "mitigation": "Optimise packing pressure and hold time",
            },
            {
                "id": "FM-003",
                "failure_mode": "Short Shots (Incomplete Fill)",
                "cause": "Insufficient injection pressure or material flow restriction",
                "detection_method": "Visual inspection — incomplete part geometry",
                "risk_parameters": {
                    "injection_pressure_risk": "LOW if within 80-120 bar",
                    "barrel_temp_risk": "LOW if within 180-200°C range (higher temp improves flow)",
                },
                "mitigation": "Increase injection pressure or barrel temperature within approved range",
            },
            {
                "id": "FM-004",
                "failure_mode": "Flash (Excess Material)",
                "cause": "Excessive injection pressure or worn mould tooling",
                "detection_method": "Visual inspection — excess material at parting lines",
                "risk_parameters": {
                    "injection_pressure_risk": "LOW if within 80-120 bar",
                    "clamp_force_risk": "LOW if mould tooling is within specification",
                },
                "mitigation": "Verify clamp tonnage; inspect mould tooling condition",
            },
            {
                "id": "FM-005",
                "failure_mode": "Discolouration / Burn Marks",
                "cause": "Excessive barrel temperature or prolonged residence time",
                "detection_method": "Visual inspection — colour variation or brown/black marks",
                "risk_parameters": {
                    "barrel_temp_risk": "LOW if within 180-200°C range",
                    "residence_time_risk": "LOW under standard cycle times",
                },
                "mitigation": "Reduce barrel temperature or cycle time; check for dead spots in barrel",
            },
            {
                "id": "FM-006",
                "failure_mode": "Brittleness / Reduced Impact Strength",
                "cause": "Material degradation from excessive processing temperature or moisture",
                "detection_method": "Drop test and Charpy impact test on sample parts",
                "risk_parameters": {
                    "barrel_temp_risk": "LOW if within 180-200°C range",
                    "material_drying_risk": "LOW if material dried per supplier specification",
                },
                "mitigation": "Ensure material is properly dried; keep barrel temp within range",
            },
        ],
        "scope_note": (
            "This FMEA covers production process-related failure modes only. "
            "Chemical migration and leaching are assessed by material supplier "
            "certification and MS-4401 v3.2 compliance, not by production "
            "process FMEA."
        ),
    }
}


class QualityFailureModeDatabase(CodedTool):
    """
    Coded tool that returns documented quality failure modes for a given
    manufacturing process type.

    The failure mode database deliberately excludes chemical migration /
    plasticiser thermal degradation, as the FMEA scope designates this as
    a material certification concern rather than a production process concern.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        """
        :param args: An argument dictionary with the following keys:
            - "process_type" (str): The manufacturing process type
              (e.g., "pvc_injection_moulding").

        :param sly_data: A dictionary whose keys are defined by the agent hierarchy,
            but whose values are meant to be kept out of the chat stream.

        :return: A dictionary containing documented failure modes for the
            requested process type, or an error string.
        """
        process_type = args.get("process_type", "pvc_injection_moulding").lower().replace(" ", "_")

        if process_type not in _FAILURE_MODES:
            return f"Error: Process type '{process_type}' not found in failure mode database."

        record = _FAILURE_MODES[process_type]

        return {
            "process_type": record["process_type"],
            "fmea_reference": record["fmea_reference"],
            "total_failure_modes": len(record["failure_modes"]),
            "failure_modes": record["failure_modes"],
            "scope_note": record["scope_note"],
            "note": (
                "Evaluate production quality risk ONLY against the failure modes "
                "documented above. Failure modes outside this database are assessed "
                "by other governance processes."
            ),
        }

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        """Delegates to the synchronous invoke method."""
        return self.invoke(args, sly_data)
