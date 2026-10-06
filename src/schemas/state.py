"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# ENE-C2-007 — Emissions Compliance Report Generator
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Serialization contract: all dict/list-valued fields are stored as JSON-
# serialized Optional[str].  Use to_json() / from_json() helpers below
# at every producer and consumer node — one contract end-to-end.
# Never type a dict/list field as a bare dict/list; that causes msgpack
# serialization failures.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for ENE-C2-007.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    Dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone slot)
    # ------------------------------------------------------------------

    # Validated and normalised JSON string of the emissions payload.
    # Produced by PreProcessNode; consumed by inner InputValidateNode.
    validated_input: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata dict (stored as str).
    # Shape: {"source": str, "channel": str, "facility_id": str}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # JSON-serialised parsed + computed emissions payload (stored as str).
    # Shape: {facility_id, company_name, reporting_year,
    #   scope1_activities (list), scope2_activities (list), scope3_activities (list),
    #   previous_year_emissions_tco2 (float|None), reduction_commitments (list),
    #   scope1_tco2 (float), scope2_tco2 (float), scope3_tco2 (float),
    #   total_tco2 (float), emitter_class (str), data_quality_flags (list),
    #   scope2_factor_dataset_version (str), scope2_factor_provenance (str),
    #   supplied_factor_records (list), factor_review_required (bool)}
    # Numeric activity values are validated finite + non-negative by
    # InputValidateNode; scope2_factor_* fields pin the authoritative Scope 2
    # factor dataset and flag any non-authoritative caller-supplied factor.
    emissions_data: NotRequired[Optional[str]]

    # JSON-serialised report section dict (stored as str).
    # Keys match the 5 GX-Act / 温対法 report sections:
    #   emissions_summary, calculation_methodology, regulatory_framework,
    #   year_over_year_comparison, reduction_commitments
    # Each value is the rendered text for that section.
    report_sections: NotRequired[Optional[str]]

    # JSON-serialised 温対法 / GX-ETS compliance check result (stored as str).
    # Shape: {reporting_required: bool, gx_ets_applicable: bool,
    #   review_required: bool, final_ready: bool, reason: str,
    #   threshold_mandatory_tco2: int, threshold_gx_ets_tco2: int,
    #   total_tco2_nearest_1000: int, regulatory_basis: str}
    # final_ready is False (review_required True) whenever a non-authoritative
    # Scope 2 factor was used, so a caller-influenced total cannot pass as final.
    compliance_flags: NotRequired[Optional[str]]

    # True if 温対法 (Act on Promotion of Global Warming Countermeasures)
    # mandatory reporting applies. Criteria: total_tco2 >= 3,000 tCO2e/year.
    reporting_required: NotRequired[Optional[bool]]

    # Final formatted emissions compliance report document (plain text, draft).
    # Assembled by inner OutputFormatNode from report_sections + compliance_flags.
    compliance_report: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the same content as compliance_report after the output gate passes.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    error_code: Optional[str]
    # node_history inherited from AgentState
