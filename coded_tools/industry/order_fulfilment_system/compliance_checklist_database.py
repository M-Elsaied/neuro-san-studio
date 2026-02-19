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


# Compliance checklist for children's toy production.
# All items verify against the same MS-4401 v3.2 standard and supplier
# certifications — which means they will all pass even when a cross-cutting
# risk (plasticiser thermal degradation) exists outside their scope.
_COMPLIANCE_CHECKLISTS = {
    "childrens_toy_production": {
        "checklist_name": "Children's Toy Production Compliance Checklist",
        "reference": "CC-TOY-2024-R2",
        "applicable_regulations": [
            "EN 71-3:2019+A1:2021 (Safety of Toys — Migration of certain elements)",
            "REACH Regulation (EC) No 1907/2006",
            "UK Toy Safety Regulations 2011 (S.I. 2011/1881)",
        ],
        "checklist_items": [
            {
                "id": "CC-001",
                "item": "Approved Vendor List (AVL) Verification",
                "description": "Confirm selected supplier is on the current Approved Vendor List",
                "verification_method": "Cross-reference supplier ID against AVL database",
                "pass_criteria": "Supplier ID exists in AVL with status APPROVED",
            },
            {
                "id": "CC-002",
                "item": "MS-4401 v3.2 Material Compliance",
                "description": "Verify material passes all 5 parameters in MS-4401 v3.2 testing standard",
                "verification_method": "Review MS-4401 v3.2 test results from supplier database",
                "pass_criteria": "All 5 tested parameters within specification",
            },
            {
                "id": "CC-003",
                "item": "Production Parameter Range Compliance",
                "description": "Verify all production parameters fall within approved ranges",
                "verification_method": "Compare planned parameters against PVR-2023-017 approved ranges",
                "pass_criteria": "All parameters within min-max approved range",
            },
            {
                "id": "CC-004",
                "item": "Quality Risk Assessment",
                "description": "Confirm quality risk assessment completed with acceptable risk levels",
                "verification_method": "Review FMEA-based quality prediction output",
                "pass_criteria": "No HIGH or CRITICAL risk failure modes identified",
            },
            {
                "id": "CC-005",
                "item": "Production Feasibility Confirmation",
                "description": "Verify production timeline meets delivery requirements",
                "verification_method": "Compare lead time and production schedule against order deadline",
                "pass_criteria": "Estimated completion date on or before required delivery date",
            },
            {
                "id": "CC-006",
                "item": "EN 71-3 Chemical Safety Compliance",
                "description": (
                    "Verify material complies with EN 71-3 migration limits for "
                    "elements in children's toys"
                ),
                "verification_method": (
                    "Material supplier certification and MS-4401 v3.2 compliance "
                    "confirmation"
                ),
                "pass_criteria": (
                    "Supplier provides EN 71-3 compliance certificate; material "
                    "passes MS-4401 v3.2"
                ),
            },
            {
                "id": "CC-007",
                "item": "REACH Regulation Compliance",
                "description": "Verify material and plasticiser comply with REACH substance restrictions",
                "verification_method": "Review supplier REACH compliance declaration and material safety data sheet",
                "pass_criteria": "No REACH-restricted substances above threshold; supplier declaration on file",
            },
        ],
    }
}


class ComplianceChecklistDatabase(CodedTool):
    """
    Coded tool that returns the compliance checklist for a given product category.

    All checklist items verify against MS-4401 v3.2, supplier certifications,
    and approved parameter ranges — the same incomplete standards used by
    upstream agents. This means all items will pass even when cross-cutting
    risks exist outside the scope of these standards.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        """
        :param args: An argument dictionary with the following keys:
            - "product_category" (str): The product category to retrieve
              the checklist for (e.g., "childrens_toy_production").

        :param sly_data: A dictionary whose keys are defined by the agent hierarchy,
            but whose values are meant to be kept out of the chat stream.

        :return: A dictionary containing the compliance checklist items for the
            requested product category, or an error string.
        """
        product_category = args.get("product_category", "childrens_toy_production").lower().replace(" ", "_")

        if product_category not in _COMPLIANCE_CHECKLISTS:
            return f"Error: Product category '{product_category}' not found in compliance checklist database."

        record = _COMPLIANCE_CHECKLISTS[product_category]

        return {
            "checklist_name": record["checklist_name"],
            "reference": record["reference"],
            "applicable_regulations": record["applicable_regulations"],
            "total_items": len(record["checklist_items"]),
            "checklist_items": record["checklist_items"],
            "note": (
                "Verify each checklist item against the outputs from upstream "
                "agents (OrderAssess, SourceSelect, PlanBuilder, QualityPredict). "
                "All items must PASS for production to be approved."
            ),
        }

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        """Delegates to the synchronous invoke method."""
        return self.invoke(args, sly_data)
