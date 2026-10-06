"""AgentCore Platform v1.0"""

# ENE-C2-007 — OutputFormatNode (inner domain node 5, last in DomainWorkflowGraph)
# Assembles the final emissions compliance report document from report_sections
# and compliance_flags.  This is the last inner node — it produces the
# compliance_report string that the outer PostProcessNode gates before the
# response reaches the caller.
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust; the invocation context passes through the
# subgraph boundary unchanged, so inner nodes must not re-raise the bar.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.precision_grid import SCHEMA_NOTE
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Section order for the final report (GX-Act / 温対法 recommended order).
_SECTION_ORDER = [
    "emissions_summary",
    "calculation_methodology",
    "regulatory_framework",
    "year_over_year_comparison",
    "reduction_commitments",
]

# Human-readable section headers.
_SECTION_HEADERS: Dict[str, str] = {
    "emissions_summary": "1. Emissions Summary (Scope 1/2/3)",
    "calculation_methodology": "2. Calculation Methodology",
    "regulatory_framework": "3. Regulatory Framework",
    "year_over_year_comparison": "4. Year-over-Year Comparison",
    "reduction_commitments": "5. Reduction Commitments",
}

# Mandatory disclaimer (draft artifact — human sign-off required).
_DISCLAIMER = (
    "This report is a draft. Final submission must be reviewed by a certified "
    "environmental consultant and approved by the compliance officer."
)

_SEPARATOR = "=" * 72
_SUBSEP = "-" * 72


def _assemble_report(
    facility_id: str,
    company_name: str,
    reporting_year: Any,
    sections: Dict[str, str],
    compliance: Dict[str, Any],
) -> str:
    """Assemble the full emissions compliance report from sections + compliance data."""
    lines = [
        _SEPARATOR,
        "EMISSIONS COMPLIANCE REPORT (DRAFT)",
        f"Company:        {company_name}",
        f"Facility ID:    {facility_id}",
        f"Reporting Year: {reporting_year}",
        _SUBSEP,
        f"Note: {SCHEMA_NOTE}",
        _SEPARATOR,
        "",
    ]

    for key in _SECTION_ORDER:
        header = _SECTION_HEADERS.get(key, key.replace("_", " ").title())
        content = sections.get(key, "(Section not generated)")
        lines.append(header)
        lines.append(_SUBSEP)
        lines.append(content)
        lines.append("")

    # Append compliance trailer.
    reporting_required = compliance.get("reporting_required", False)
    gx_ets = compliance.get("gx_ets_applicable", False)
    reason = compliance.get("reason", "N/A")
    regulatory = compliance.get(
        "regulatory_basis",
        "地球温暖化対策の推進に関する法律 (温対法) 算定・報告・公表制度",
    )
    review_required = compliance.get("review_required", False)
    # final_ready defaults True for backward compatibility with a compliance dict
    # that predates the review-status fields.
    final_ready = compliance.get("final_ready", not review_required)
    lines += [
        _SEPARATOR,
        "REGULATORY COMPLIANCE NOTE",
        _SUBSEP,
        f"  Regulatory Basis:            {regulatory}",
        f"  温対法 Reporting Required:    {'YES' if reporting_required else 'NO'}",
        f"  GX-ETS Participation Scope:  {'YES' if gx_ets else 'NO'}",
        f"  Final-Ready (submittable):   "
        f"{'YES' if final_ready else 'NO — human review of Scope 2 factor provenance required'}",
        f"  Determination Basis:         {reason}",
        _SEPARATOR,
        "",
        "DISCLAIMER",
        _SUBSEP,
        _DISCLAIMER,
        _SEPARATOR,
    ]

    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the final emissions compliance report document (inner domain node).

    Reads report_sections and compliance_flags from State, renders the full
    GX-Act / 温対法 compliant emissions report text (with the precision schema
    note and the mandatory draft disclaimer), and writes it to
    compliance_report (and result) for the outer PostProcessNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        report_sections:  str  — JSON-serialised section dict
        compliance_flags: str  — JSON-serialised compliance result
        emissions_data:   str  — JSON-serialised emissions payload

    Output state keys (partial dict):
        compliance_report: str
        result:            str  (same as compliance_report — backbone convention)
        status:            str
        error_log:         list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        sections: Dict[str, str] = from_json(state.get("report_sections"), {})
        compliance: Dict[str, Any] = from_json(state.get("compliance_flags"), {})
        emissions_data: Dict[str, Any] = from_json(state.get("emissions_data"), {})

        facility_id = emissions_data.get("facility_id", "unknown")
        company_name = emissions_data.get("company_name", facility_id)
        reporting_year = emissions_data.get("reporting_year", "N/A")

        if not sections:
            logger.error(
                "OutputFormatNode: report_sections missing in state for facility_id=%s",
                facility_id,
            )
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_report_sections", "facility_id": facility_id},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputFormatNode: report_sections missing for facility_id={facility_id}"],
            }

        # ── Assemble the report ───────────────────────────────────────────────
        report = _assemble_report(facility_id, company_name, reporting_year, sections, compliance)

        logger.info(
            "OutputFormatNode: facility_id=%s report_chars=%d reporting_required=%s",
            facility_id,
            len(report),
            compliance.get("reporting_required", False),
        )
        emit_trace_event(
            "output_format_complete",
            {
                "facility_id": facility_id,
                "report_length": len(report),
                "section_count": len(sections),
                "reporting_required": compliance.get("reporting_required", False),
            },
            state,
        )

        return {
            "compliance_report": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
