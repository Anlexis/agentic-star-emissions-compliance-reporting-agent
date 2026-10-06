"""AgentCore Platform v1.0"""

# ENE-C2-007 — ComplianceCheckNode
# Inner domain node 4: 温対法 / GX-ETS mandatory-reporting compliance check.
#
# Determines whether the facility's total emissions meet the regulatory
# reporting thresholds requiring:
#   - 温対法 算定・報告・公表制度 annual report  (>= 3,000 tCO2e/year), and
#   - GX-ETS participation                       (>= 100,000 tCO2e/year).
#
# The threshold DETERMINATION runs on the full-precision internal total; the
# determination the caller sees (reason text + total field) renders on the
# external precision grid (nearest 1,000 tCO2e) — the full-precision aggregate
# never leaves the internal state.
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

from src.nodes.precision_grid import format_grid, to_grid
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# 温対法 / GX-ETS reporting thresholds (total tCO2e / year).
_MANDATORY_REPORT_THRESHOLD_TCO2 = 3_000  # 温対法 特定排出者 annual-report threshold
_GX_ETS_THRESHOLD_TCO2 = 100_000  # GX-ETS large-emitter threshold


def _determine_reportability(total_tco2: float, factor_review_required: bool = False) -> Dict[str, Any]:
    """Evaluate 温対法 / GX-ETS reportability criteria.

    Returns a dict with reporting_required, gx_ets_applicable, reason, the
    threshold values, and a final-ready / review-required status for full
    traceability in the report's regulatory section. The boolean
    determinations compare the FULL-PRECISION total against the thresholds;
    the total and reason text placed in the returned dict are rendered on the
    external grid (nearest 1,000 tCO2e) because this dict is surfaced to the
    caller.

    factor_review_required: when a non-authoritative Scope 2 emission factor
    was used upstream, the numeric determination cannot be treated as final —
    final_ready is forced False and review_required True, so a
    caller-influenced total cannot silently pass as a final "no reporting
    required" compliance decision.
    """
    reporting_required = total_tco2 >= _MANDATORY_REPORT_THRESHOLD_TCO2
    gx_ets_applicable = total_tco2 >= _GX_ETS_THRESHOLD_TCO2

    total_display = format_grid(total_tco2)
    reasons = []
    if reporting_required:
        reasons.append(
            f"Total emissions ({total_display} tCO2e, nearest 1,000) >= "
            f"温対法 threshold ({_MANDATORY_REPORT_THRESHOLD_TCO2:,} tCO2e)"
        )
    else:
        reasons.append(
            f"Total emissions ({total_display} tCO2e, nearest 1,000) < "
            f"温対法 threshold ({_MANDATORY_REPORT_THRESHOLD_TCO2:,} tCO2e)"
        )
    if gx_ets_applicable:
        reasons.append(
            f"Total emissions ({total_display} tCO2e, nearest 1,000) >= "
            f"GX-ETS threshold ({_GX_ETS_THRESHOLD_TCO2:,} tCO2e)"
        )

    review_required = bool(factor_review_required)
    if review_required:
        reasons.append(
            "NON-FINAL: a non-authoritative Scope 2 emission factor was used — "
            "human review of factor provenance is required before submission"
        )

    return {
        "reporting_required": reporting_required,
        "gx_ets_applicable": gx_ets_applicable,
        "review_required": review_required,
        "final_ready": not review_required,
        "reason": "; ".join(reasons),
        "threshold_mandatory_tco2": _MANDATORY_REPORT_THRESHOLD_TCO2,
        "threshold_gx_ets_tco2": _GX_ETS_THRESHOLD_TCO2,
        "total_tco2_nearest_1000": to_grid(total_tco2),
        "regulatory_basis": (
            "地球温暖化対策の推進に関する法律 (温対法) 算定・報告・公表制度 / " "GX推進法 (2023) GX-ETS"
        ),
    }


class ComplianceCheckNode(FunctionNode):
    """温対法 / GX-ETS mandatory-reporting compliance check for ENE-C2-007.

    Evaluates whether the facility requires 温対法 annual reporting and/or
    GX-ETS participation, and produces a compliance_flags dict with full
    traceability data for inclusion in the report's regulatory section.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        emissions_data: str  — JSON-serialised enriched emissions payload

    Output state keys (partial dict):
        compliance_flags:   str   — JSON-serialised compliance result dict
        reporting_required: bool  — True if 温対法 reporting is required
        status:             str
        error_log:          list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        emissions_data: Dict[str, Any] = from_json(state.get("emissions_data"), {})

        if not emissions_data:
            logger.error("ComplianceCheckNode: emissions_data missing in state")
            emit_trace_event(
                "compliance_check_failed",
                {"reason": "missing_emissions_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ComplianceCheckNode: emissions_data missing in state"],
            }

        facility_id = emissions_data.get("facility_id", "unknown")
        try:
            total_tco2 = float(emissions_data.get("total_tco2", 0.0))
        except (TypeError, ValueError):
            total_tco2 = 0.0
        factor_review_required = bool(emissions_data.get("factor_review_required", False))

        # ── 温対法 / GX-ETS evaluation ────────────────────────────────────────
        compliance_result = _determine_reportability(total_tco2, factor_review_required)
        reporting_required = bool(compliance_result["reporting_required"])

        logger.info(
            "ComplianceCheckNode: facility_id=%s total=%.3f tCO2 " "reporting_required=%s gx_ets=%s final_ready=%s",
            facility_id,
            total_tco2,
            reporting_required,
            compliance_result["gx_ets_applicable"],
            compliance_result["final_ready"],
        )
        emit_trace_event(
            "compliance_check_complete",
            {
                "facility_id": facility_id,
                "total_tco2": total_tco2,
                "reporting_required": reporting_required,
                "gx_ets_applicable": compliance_result["gx_ets_applicable"],
                "final_ready": compliance_result["final_ready"],
            },
            state,
        )

        return {
            "compliance_flags": to_json(compliance_result),
            "reporting_required": reporting_required,
            "status": AgentStatus.SUCCESS.value,
        }
