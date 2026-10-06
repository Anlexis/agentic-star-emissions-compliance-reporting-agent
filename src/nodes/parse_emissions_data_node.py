"""AgentCore Platform v1.0"""

# ENE-C2-007 — ParseEmissionsDataNode
# Inner domain node 2: compute Scope 1/2/3 emissions and classify the emitter.
#
# Responsibilities:
#   - Apply 環境省 (Ministry of the Environment) emission factors to Scope 1
#     fuel activity data and Scope 2 electricity/heat activity data
#   - Sum caller-provided Scope 3 emissions
#   - Derive total tCO2e and an emitter-size classification
#   - Flag unknown fuel/energy types for data-quality transparency
#   - Annotate emissions_data with the derived totals for downstream nodes
#
# Scope 2 emission factors:
#   Scope 2 factors are sourced from a VERSION-PINNED authoritative dataset
#   (_SCOPE2_AUTHORITATIVE_FACTORS, keyed by energy type + unit, with a dataset
#   version + provenance). A caller-supplied factor can NEVER override an
#   authoritative one (so a fabricated low factor cannot lower a regulated
#   result). A non-listed energy source may only use a caller-supplied factor
#   WITH an explicit provenance record; either way, using any non-authoritative
#   factor sets factor_review_required=True, which the downstream compliance
#   node turns into a non-final (human-review-required) determination.
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust; the invocation context passes through the
# subgraph boundary unchanged, so inner nodes must not re-raise the bar.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# 環境省 温対法 representative emission factors (tCO2 per unit of activity).
# These mirror the publicly published 算定・報告・公表制度 coefficients used for
# 温対法 reporting.  Production wires the annually-updated factor tables.
_SCOPE1_FUEL_FACTORS: Dict[str, float] = {
    "city_gas": 0.00229,  # tCO2 / m3
    "lng": 0.00278,  # tCO2 / kg
    "diesel": 0.00258,  # tCO2 / L
    "gasoline": 0.00232,  # tCO2 / L
    "kerosene": 0.00249,  # tCO2 / L
    "heavy_oil": 0.00294,  # tCO2 / L
    "lpg": 0.00300,  # tCO2 / kg
    "coal": 2.33,  # tCO2 / t
}

# ── Scope 2 authoritative, version-pinned emission-factor dataset ──────────────
# Scope 2 factors must come from a pinned authoritative table (keyed by energy
# type + unit), not from unchecked caller input. Provenance / version are
# recorded internally for every computation.
# Production wires the annually-published 環境省・経済産業省 coefficient tables here.
_SCOPE2_FACTOR_DATASET_VERSION = "MOE-METI-SANHO-2024.1"
_SCOPE2_FACTOR_PROVENANCE = "環境省・経済産業省 算定・報告・公表制度 電気・熱 排出係数 (FY2024 公表値)"
# energy_type -> (factor tCO2/unit, unit)
_SCOPE2_AUTHORITATIVE_FACTORS: Dict[str, Tuple[float, str]] = {
    "grid_electricity": (0.000434, "kWh"),  # Japan national grid average
    "electricity": (0.000434, "kWh"),  # alias of grid_electricity
    "steam": (0.0000600, "MJ"),  # 産業用蒸気 representative factor
    "industrial_steam": (0.0000600, "MJ"),
    "hot_water": (0.0000570, "MJ"),  # 温水 representative factor
    "cold_water": (0.0000570, "MJ"),  # 冷水 representative factor
}
# Energy-type spellings that default to grid electricity when unspecified.
_GRID_ELECTRICITY_ALIASES = ("grid_electricity", "electricity", "")

# Emitter-size thresholds (total tCO2e / year).
_GX_ETS_THRESHOLD_TCO2 = 100_000  # GX-ETS large-emitter scope
_MANDATORY_REPORT_THRESHOLD_TCO2 = 3_000  # 温対法 特定排出者 threshold


def _to_float(value: Any) -> float:
    """Best-effort float coercion (numeric validity is enforced upstream in
    InputValidateNode; this is a defensive fallback only)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _compute_scope1(activities: List[Dict[str, Any]]) -> Tuple[float, List[str]]:
    """Compute Scope 1 emissions from fuel activity data. Returns (tco2, flags)."""
    total = 0.0
    flags: List[str] = []
    for act in activities:
        fuel = str(act.get("fuel_type", "")).lower().strip()
        qty = _to_float(act.get("quantity", 0))
        factor = _SCOPE1_FUEL_FACTORS.get(fuel)
        if factor is None:
            flags.append(f"unknown_scope1_fuel_type:{fuel or 'blank'}")
            continue
        total += qty * factor
    return round(total, 3), flags


def _compute_scope2(activities: List[Dict[str, Any]]) -> Tuple[float, List[str], List[Dict[str, Any]], bool]:
    """Compute Scope 2 emissions from the authoritative factor dataset.

    Returns (tco2, flags, supplied_factor_records, factor_review_required).

    Rules:
      - Known energy types use the version-pinned authoritative factor. A
        caller-supplied factor for a known type is IGNORED (it cannot override /
        lower a regulated factor) and flagged for transparency.
      - A non-listed energy type may only use a caller-supplied factor. Using one
        records its provenance (or flags its absence) and sets
        factor_review_required=True → the compliance node marks the result as
        non-final (human review required before submission).
    """
    total = 0.0
    flags: List[str] = []
    supplied_factor_records: List[Dict[str, Any]] = []
    review_required = False

    for act in activities:
        energy = str(act.get("energy_type", "grid_electricity")).lower().strip()
        if energy == "":
            energy = "grid_electricity"
        qty = _to_float(act.get("quantity", 0))  # kWh (electricity) / MJ (heat)
        supplied_raw = act.get("emission_factor_tco2_per_unit")
        has_supplied = supplied_raw not in (None, "")

        authoritative = _SCOPE2_AUTHORITATIVE_FACTORS.get(energy)
        if authoritative is not None:
            factor, _unit = authoritative
            if has_supplied:
                # Caller cannot override an authoritative factor.
                flags.append(f"scope2_caller_factor_ignored_authoritative_used:{energy}")
            total += qty * factor
            continue

        # Non-listed energy source — no authoritative factor available.
        if not has_supplied:
            flags.append(f"unknown_scope2_energy_type:{energy}")
            continue

        supplied = _to_float(supplied_raw)
        provenance = str(act.get("emission_factor_provenance", "")).strip()
        review_required = True  # any non-authoritative factor → non-final result
        if provenance:
            flags.append(f"scope2_supplied_factor_pending_review:{energy}")
            supplied_factor_records.append(
                {
                    "energy_type": energy,
                    "factor_tco2_per_unit": supplied,
                    "provenance": provenance,
                    "status": "supplied_pending_review",
                }
            )
        else:
            # Unapproved: a supplied factor with no provenance cannot yield a
            # final compliance determination.
            flags.append(f"unapproved_scope2_factor:{energy}")
            supplied_factor_records.append(
                {
                    "energy_type": energy,
                    "factor_tco2_per_unit": supplied,
                    "provenance": None,
                    "status": "unapproved_pending_review",
                }
            )
        total += qty * supplied

    return round(total, 3), flags, supplied_factor_records, review_required


def _compute_scope3(activities: List[Dict[str, Any]]) -> Tuple[float, List[str]]:
    """Sum caller-provided Scope 3 emissions. Returns (tco2, flags)."""
    total = 0.0
    flags: List[str] = []
    for act in activities:
        emissions = _to_float(act.get("emissions_tco2", 0))
        if emissions <= 0:
            flags.append(f"scope3_missing_emissions:{act.get('category', 'uncategorised')}")
        total += emissions
    return round(total, 3), flags


def _classify_emitter(total_tco2: float) -> str:
    """Classify the emitter by total tCO2e/year."""
    if total_tco2 >= _GX_ETS_THRESHOLD_TCO2:
        return "large_emitter_gx_ets"
    if total_tco2 >= _MANDATORY_REPORT_THRESHOLD_TCO2:
        return "specified_emitter"
    return "below_mandatory_threshold"


class ParseEmissionsDataNode(FunctionNode):
    """Compute Scope 1/2/3 emissions and classify the emitter.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        emissions_data: str  — JSON-serialised emissions payload

    Output state keys (partial dict):
        emissions_data: str  — enriched JSON string, same key updated
        status:         str
        error_log:      list[str]  (only on ERROR)
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
            logger.error("ParseEmissionsDataNode: emissions_data is empty or missing")
            emit_trace_event(
                "parse_emissions_data_failed",
                {"reason": "missing_emissions_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseEmissionsDataNode: emissions_data missing in state"],
            }

        facility_id = emissions_data.get("facility_id", "unknown")

        scope1_tco2, s1_flags = _compute_scope1(emissions_data.get("scope1_activities", []))
        scope2_tco2, s2_flags, s2_factor_records, s2_review_required = _compute_scope2(
            emissions_data.get("scope2_activities", [])
        )
        scope3_tco2, s3_flags = _compute_scope3(emissions_data.get("scope3_activities", []))

        total_tco2 = round(scope1_tco2 + scope2_tco2 + scope3_tco2, 3)
        emitter_class = _classify_emitter(total_tco2)
        data_quality_flags = s1_flags + s2_flags + s3_flags

        enriched: Dict[str, Any] = dict(emissions_data)
        enriched["scope1_tco2"] = scope1_tco2
        enriched["scope2_tco2"] = scope2_tco2
        enriched["scope3_tco2"] = scope3_tco2
        enriched["total_tco2"] = total_tco2
        enriched["emitter_class"] = emitter_class
        enriched["data_quality_flags"] = data_quality_flags
        # Scope 2 factor provenance / version + review status.
        enriched["scope2_factor_dataset_version"] = _SCOPE2_FACTOR_DATASET_VERSION
        enriched["scope2_factor_provenance"] = _SCOPE2_FACTOR_PROVENANCE
        enriched["supplied_factor_records"] = s2_factor_records
        enriched["factor_review_required"] = bool(s2_review_required)

        logger.info(
            "ParseEmissionsDataNode: facility_id=%s total=%.3f tCO2 "
            "(s1=%.3f s2=%.3f s3=%.3f) class=%s flags=%d review_required=%s",
            facility_id,
            total_tco2,
            scope1_tco2,
            scope2_tco2,
            scope3_tco2,
            emitter_class,
            len(data_quality_flags),
            s2_review_required,
        )
        emit_trace_event(
            "parse_emissions_data_complete",
            {
                "facility_id": facility_id,
                "total_tco2": total_tco2,
                "emitter_class": emitter_class,
                "data_quality_flag_count": len(data_quality_flags),
                "factor_review_required": bool(s2_review_required),
            },
            state,
        )

        return {
            "emissions_data": to_json(enriched),
            "status": AgentStatus.SUCCESS.value,
        }
