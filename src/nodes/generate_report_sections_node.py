"""AgentCore Platform v1.0"""

# ENE-C2-007 — GenerateReportSectionsNode
# Inner domain node 3: generate all 5 emissions-compliance report sections.
#
# v1 implementation: deterministic template-based generation.
# Production wires the real LLM here via the constructor-injected config
# (self._config["configurable"]) — execute(self, state) takes no config
# parameter; configuration is injected through the node constructor by the
# graph. In v1 the section text is synthesised from the emissions_data fields
# using fixed templates.
#
# External precision contract: every emissions figure rendered into a section
# is an AGGREGATE on the external grid (nearest 1,000 tCO2e — see
# src/nodes/precision_grid.py). Individual activity records (fuel quantities,
# caller-supplied factors, provenance text) are never reproduced in a section
# — the report names WHICH sources need review, not their values.
#
# Sections produced:
#   1. emissions_summary          (Scope 1/2/3 grid-rounded summary)
#   2. calculation_methodology    (emission factors + method basis)
#   3. regulatory_framework       (温対法 / GX Act citations)
#   4. year_over_year_comparison  (vs previous_year_emissions_tco2)
#   5. reduction_commitments      (stated reduction commitments)
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust; the invocation context passes through the
# subgraph boundary unchanged, so inner nodes must not re-raise the bar.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.precision_grid import format_grid, to_grid
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)


def _section_emissions_summary(d: Dict[str, Any]) -> str:
    """Generate the Scope 1/2/3 emissions summary section (grid-rounded)."""
    year = d.get("reporting_year", "N/A")
    s1 = format_grid(float(d.get("scope1_tco2", 0.0)))
    s2 = format_grid(float(d.get("scope2_tco2", 0.0)))
    s3 = format_grid(float(d.get("scope3_tco2", 0.0)))
    total = format_grid(float(d.get("total_tco2", 0.0)))
    lines = [
        f"Reporting Year: {year}",
        f"Scope 1 (direct):           {s1} tCO2e",
        f"Scope 2 (purchased energy): {s2} tCO2e",
        f"Scope 3 (value chain):      {s3} tCO2e",
        f"Total Emissions:            {total} tCO2e",
        f"Emitter Classification:     {d.get('emitter_class', 'unknown')}",
        "(All figures rounded to the nearest 1,000 tCO2e.)",
    ]
    return "\n".join(lines)


def _section_calculation_methodology(d: Dict[str, Any]) -> str:
    """Generate the calculation methodology section.

    Renders the authoritative factor dataset identity and data-quality notes.
    Caller-supplied factor VALUES and provenance text are recorded internally
    but never reproduced here — only the affected energy source and its
    review status appear.
    """
    factor_version = d.get("scope2_factor_dataset_version", "N/A")
    factor_provenance = d.get(
        "scope2_factor_provenance",
        "環境省・経済産業省 算定・報告・公表制度 電気・熱 排出係数",
    )
    lines = [
        "Emissions are calculated under the GHG Protocol Corporate Standard,",
        "using 環境省 温対法 算定・報告・公表制度 emission factors:",
        "  - Scope 1: activity quantity × fuel-specific emission factor (tCO2/unit).",
        "  - Scope 2: activity quantity × the authoritative electricity/heat factor",
        f"    from the version-pinned dataset {factor_version}",
        f"    (source: {factor_provenance}); grid electricity uses the Japan",
        "    national grid average. A caller-supplied factor can never override",
        "    an authoritative one.",
        "  - Scope 3: caller-provided category emissions (tCO2e), summed.",
        "  - Reported figures are aggregates rounded to the nearest 1,000 tCO2e;",
        "    individual activity records are not reproduced in this report.",
    ]
    if d.get("factor_review_required"):
        lines.append("")
        lines.append("  ⚠ A non-authoritative Scope 2 emission factor was supplied for a " "non-listed energy source.")
        lines.append(
            "    This report is NOT final-ready: the supplied factor's provenance "
            "must be reviewed and approved before regulatory submission."
        )
        for rec in d.get("supplied_factor_records", []) or []:
            lines.append(f"    - {rec.get('energy_type')}: caller-supplied factor " f"[{rec.get('status')}]")
    flags: List[str] = d.get("data_quality_flags", []) or []
    if flags:
        lines.append("")
        lines.append("Data quality notes (review before final submission):")
        for f in flags:
            lines.append(f"  - {f}")
    return "\n".join(lines)


def _section_regulatory_framework(d: Dict[str, Any]) -> str:
    """Generate the regulatory framework citation section."""
    lines = [
        "Applicable regulatory frameworks:",
        "  - 地球温暖化対策の推進に関する法律 (温対法) — 算定・報告・公表制度:",
        "    specified emitters (>= 3,000 tCO2e/year) must report annually to",
        "    the competent minister via 経済産業省 / 環境省.",
        "  - GX推進法 (2023) / GX-ETS: large emitters (>= 100,000 tCO2e/year)",
        "    participate in the emissions-trading scheme from FY2026.",
        "  - TCFD-aligned disclosure applies to TSE Prime-listed companies.",
    ]
    return "\n".join(lines)


def _section_year_over_year(d: Dict[str, Any]) -> str:
    """Generate the year-over-year comparison section (grid-rounded).

    The comparison is computed FROM the grid-rounded aggregates so the
    rendered delta and percentage are consistent with the figures shown —
    and so the full-precision internal totals never surface here.
    """
    prev: Any = d.get("previous_year_emissions_tco2")
    if prev is None or prev == 0:
        return (
            "No prior-year emissions figure was provided. Year-over-year "
            "comparison will be available once a baseline year is recorded."
        )
    total_grid = to_grid(float(d.get("total_tco2", 0.0)))
    prev_grid = to_grid(float(prev))
    delta = total_grid - prev_grid
    pct = round((delta / prev_grid) * 100, 1) if prev_grid else 0.0
    direction = "reduction" if delta < 0 else ("increase" if delta > 0 else "no change")
    lines = [
        f"Previous Year:  {prev_grid:,d} tCO2e",
        f"Current Year:   {total_grid:,d} tCO2e",
        f"Change:         {delta:+,d} tCO2e ({pct:+.1f}%) — {direction}",
        "(All figures rounded to the nearest 1,000 tCO2e.)",
    ]
    return "\n".join(lines)


def _section_reduction_commitments(d: Dict[str, Any]) -> str:
    """Generate the reduction commitments section."""
    commitments: List[Any] = d.get("reduction_commitments", []) or []
    if not commitments:
        return (
            "No reduction commitments were provided. Under the GX roadmap toward "
            "Carbon Neutrality 2050, a documented reduction pathway is recommended."
        )
    return "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(commitments))


class GenerateReportSectionsNode(FunctionNode):
    """Generate all 5 GX-Act / 温対法 compliance report sections.

    v1: deterministic template-based synthesis, rendered on the external
    precision grid (nearest 1,000 tCO2e).
    Production: the constructor-injected config's ``configurable`` block
    (system prompt template, temperature, max_tokens) drives an LLM.

    Config is injected through the constructor — execute() takes no config
    parameter. DomainWorkflowGraph.register_nodes() passes the graph's config
    dict, forwarded from EmissionsReportGraphNode (which carries the values
    loaded from config/config.yaml).

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        emissions_data: str  — JSON-serialised enriched emissions payload

    Output state keys (partial dict):
        report_sections: str  — JSON-serialised section dict
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Store the graph-level config dict (constructor injection)."""
        self._config = config

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        emissions_data: Dict[str, Any] = from_json(state.get("emissions_data"), {})

        # Generation settings from the constructor-injected config
        # (production wires the real LLM here; v1 synthesis is deterministic).
        configurable = (self._config or {}).get("configurable", {})
        _system_prompt_template = configurable.get("system_prompt_template", "")  # noqa: F841

        if not emissions_data:
            logger.error("GenerateReportSectionsNode: emissions_data missing in state")
            emit_trace_event(
                "generate_report_sections_failed",
                {"reason": "missing_emissions_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateReportSectionsNode: emissions_data missing in state"],
            }

        facility_id = emissions_data.get("facility_id", "unknown")

        # ── Generate all sections ─────────────────────────────────────────────
        sections: Dict[str, str] = {
            "emissions_summary": _section_emissions_summary(emissions_data),
            "calculation_methodology": _section_calculation_methodology(emissions_data),
            "regulatory_framework": _section_regulatory_framework(emissions_data),
            "year_over_year_comparison": _section_year_over_year(emissions_data),
            "reduction_commitments": _section_reduction_commitments(emissions_data),
        }

        logger.info(
            "GenerateReportSectionsNode: facility_id=%s sections=%d",
            facility_id,
            len(sections),
        )
        emit_trace_event(
            "generate_report_sections_complete",
            {
                "facility_id": facility_id,
                "section_count": len(sections),
                "section_keys": sorted(sections.keys()),
            },
            state,
        )

        return {
            "report_sections": to_json(sections),
            "status": AgentStatus.SUCCESS.value,
        }
