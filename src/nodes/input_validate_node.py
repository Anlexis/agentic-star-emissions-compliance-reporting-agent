"""AgentCore Platform v1.0"""

# ENE-C2-007 — InputValidateNode
# Inner domain node 1: domain-level validation of the emissions payload.
#
# Distinct from PreProcessNode (trust enforcement + structural JSON check):
# this node applies the caller-data contract — every caller-controlled value
# is hostile until proven bounded:
#
#   * every numeric field goes through a finite+bounded parser (rejects bools,
#     non-numerics, NaN / +Infinity / -Infinity — which parse via float() AND
#     arrive via raw JSON, and whose comparisons are always False — and
#     out-of-range magnitudes). Fail CLOSED with a field-naming error;
#   * every string that can reach the rendered report is either locked to an
#     inert identifier pattern or bounded to single-line printable text;
#   * structural limits cap list sizes so a payload cannot balloon the
#     pipeline;
#   * rejected values are NEVER echoed into error logs — errors name the
#     field (and index), not the value;
#   * activity records are whitelist-copied: only the documented keys survive
#     into the internal emissions_data, so undocumented caller keys cannot
#     smuggle content downstream.
#
# A malformed, non-finite, or negative measurement must NOT silently flow into
# the emissions total (where NaN/negatives can flip reporting_required to
# False). Absent/blank numeric fields default to 0 downstream (a legitimately
# zero-activity source), which is not an error.
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust, and the invocation context passes through the
# subgraph boundary unchanged, so inner nodes must not re-raise the bar.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED, INVALID_VALUE

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Plausible reporting-year window (mandatory GHG reporting in Japan began in
# FY2006; guard against obvious garbage / far-future values while staying
# tolerant of back-filing).
_MIN_REPORTING_YEAR = 2005
_MAX_REPORTING_YEAR = 2100

# ── Structural limits (caps, not business rules) ──────────────────────────────
_MAX_ACTIVITIES_PER_SCOPE = 500
_MAX_REDUCTION_COMMITMENTS = 20

# ── String contracts ─────────────────────────────────────────────────────────
# Identifiers are locked to an inert character set — they render into the
# report and into data-quality flags, so free text there would be
# caller-controlled output injection.
_FACILITY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TYPE_TOKEN_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Bounded free-text fields: single line, printable, length-capped. These are
# the only caller strings allowed to carry prose into the report.
_MAX_COMPANY_NAME_LEN = 120
_MAX_COMMITMENT_LEN = 300
_MAX_PROVENANCE_LEN = 200

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# ── Numeric bounds per field (finite + non-negative + magnitude-capped) ───────
# field name -> (upper bound). All lower bounds are 0 (measurements cannot be
# negative). Upper bounds are generous physical-plausibility caps, not
# business thresholds.
_MAX_ACTIVITY_QUANTITY = 1e12  # fuel/energy activity quantity per record
_MAX_EMISSION_FACTOR = 1e4  # tCO2 per unit — real factors are << 10
_MAX_EMISSIONS_TCO2 = 1e9  # per-record and prior-year totals

# scope key -> ((field, upper_bound), ...) for PRESENT-value validation.
_SCOPE_NUMERIC_FIELDS: Dict[str, Tuple[Tuple[str, float], ...]] = {
    "scope1_activities": (("quantity", _MAX_ACTIVITY_QUANTITY),),
    "scope2_activities": (
        ("quantity", _MAX_ACTIVITY_QUANTITY),
        ("emission_factor_tco2_per_unit", _MAX_EMISSION_FACTOR),
    ),
    "scope3_activities": (("emissions_tco2", _MAX_EMISSIONS_TCO2),),
}

# Whitelist-copied keys per activity record: undocumented caller keys are
# dropped at the boundary.
_SCOPE_ALLOWED_KEYS: Dict[str, Tuple[str, ...]] = {
    "scope1_activities": ("fuel_type", "quantity", "unit"),
    "scope2_activities": (
        "energy_type",
        "quantity",
        "unit",
        "emission_factor_tco2_per_unit",
        "emission_factor_provenance",
    ),
    "scope3_activities": ("category", "emissions_tco2"),
}

# Type-token field per scope (locked to _TYPE_TOKEN_RE after lower/strip).
_SCOPE_TYPE_FIELD: Dict[str, str] = {
    "scope1_activities": "fuel_type",
    "scope2_activities": "energy_type",
    "scope3_activities": "category",
}


def _finite_in_range(raw: Any, upper: float) -> Tuple[bool, float]:
    """Parse a caller-controlled number: finite, non-negative, magnitude-capped.

    Returns (ok, value). Rejects bools (an int subclass), non-numeric values,
    NaN / +-Infinity, negatives, and values above `upper`. Fail CLOSED: any
    parse doubt is a rejection.
    """
    if isinstance(raw, bool):
        return False, 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False, 0.0
    if not math.isfinite(value):
        return False, 0.0
    if value < 0 or value > upper:
        return False, 0.0
    return True, value


def _bounded_line(raw: Any, max_len: int) -> Optional[str]:
    """Validate a bounded single-line printable string; None on rejection."""
    if not isinstance(raw, str):
        return None
    if len(raw) > max_len:
        return None
    if _CONTROL_CHARS_RE.search(raw):
        return None
    return raw


def _normalise_activities(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Coerce an activity list into a list of dicts.

    Returns None when the value is not a list or exceeds the structural cap;
    non-dict entries are dropped (an absent record, not an error).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    if len(raw) > _MAX_ACTIVITIES_PER_SCOPE:
        return None
    return [dict(item) for item in raw if isinstance(item, dict)]


def _validate_scope_activities(activities: List[Dict[str, Any]], scope: str) -> Optional[str]:
    """Validate and whitelist-copy one scope's activity records IN PLACE.

    Returns an error string (field-naming only — never the value) on the
    first invalid record, or None when every record conforms.
    """
    allowed = _SCOPE_ALLOWED_KEYS[scope]
    type_field = _SCOPE_TYPE_FIELD[scope]
    for idx, act in enumerate(activities):
        # Whitelist copy: undocumented keys are dropped at the boundary.
        cleaned = {k: act[k] for k in allowed if k in act}

        # Type token (fuel_type / energy_type / category): inert identifier.
        token = cleaned.get(type_field)
        if token is not None:
            if not isinstance(token, str):
                return f"{scope}[{idx}].{type_field} must be a string identifier"
            normalised = token.lower().strip()
            if normalised and not _TYPE_TOKEN_RE.match(normalised):
                return f"{scope}[{idx}].{type_field} must match " f"[a-z0-9_]{{1,32}} after normalisation"
            cleaned[type_field] = normalised

        # Numeric fields: finite, non-negative, magnitude-capped.
        for field, upper in _SCOPE_NUMERIC_FIELDS[scope]:
            if field not in cleaned:
                continue
            raw = cleaned[field]
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                continue
            ok, value = _finite_in_range(raw, upper)
            if not ok:
                return f"{scope}[{idx}].{field} must be a finite, non-negative " f"number within bounds"
            cleaned[field] = value

        # Bounded provenance text (scope 2 only).
        if "emission_factor_provenance" in cleaned:
            prov = _bounded_line(cleaned["emission_factor_provenance"], _MAX_PROVENANCE_LEN)
            if prov is None:
                return (
                    f"{scope}[{idx}].emission_factor_provenance must be a "
                    f"single-line string of at most {_MAX_PROVENANCE_LEN} characters"
                )
            cleaned["emission_factor_provenance"] = prov

        act.clear()
        act.update(cleaned)
    return None


class InputValidateNode(FunctionNode):
    """Domain validation of the emissions payload for ENE-C2-007.

    Applies the caller-data contract beyond the structural JSON check in
    PreProcessNode: finite+bounded numerics, inert identifiers, bounded
    free-text fields, structural caps, and presence of at least one emissions
    source. Every rejection fails closed and names only the offending field.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input: str  — normalised JSON string from PreProcessNode
                                Falls back to user_input for unit-test convenience.

    Output state keys (partial dict):
        emissions_data: str   — JSON-serialised normalised emissions payload
        status:         str
        error_log:      list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            payload: Dict[str, Any] = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError):
            logger.error("InputValidateNode: JSON parse error")
            emit_trace_event(
                "input_validate_failed",
                {"reason": "json_parse_error"},
                state,
            )
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: payload is not parseable JSON"],
            }

        if not isinstance(payload, dict):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "payload_not_dict"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: payload is not a JSON object"],
            }

        # ── facility_id: inert identifier ─────────────────────────────────────
        facility_id = payload.get("facility_id")
        if not isinstance(facility_id, str) or not _FACILITY_ID_RE.match(facility_id):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "invalid_facility_id"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: facility_id must match [A-Za-z0-9_-]{1,64}"],
            }

        # ── company_name: bounded single-line text ────────────────────────────
        company_raw = payload.get("company_name", "")
        if company_raw in (None, ""):
            company_name = facility_id
        else:
            bounded = _bounded_line(company_raw, _MAX_COMPANY_NAME_LEN)
            if bounded is None or not bounded.strip():
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "invalid_company_name"},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [
                        "InputValidateNode: company_name must be a single-line "
                        f"string of at most {_MAX_COMPANY_NAME_LEN} characters"
                    ],
                }
            company_name = bounded.strip()

        # ── Reporting year sanity ─────────────────────────────────────────────
        year_raw = payload.get("reporting_year", 0)
        if isinstance(year_raw, bool):
            year_raw = 0
        try:
            reporting_year = int(year_raw)
        except (TypeError, ValueError):
            reporting_year = 0
        if not (_MIN_REPORTING_YEAR <= reporting_year <= _MAX_REPORTING_YEAR):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "invalid_reporting_year"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    f"InputValidateNode: reporting_year out of range " f"[{_MIN_REPORTING_YEAR}-{_MAX_REPORTING_YEAR}]"
                ],
            }

        # ── Normalise scope activities (structural caps) ──────────────────────
        scopes: Dict[str, List[Dict[str, Any]]] = {}
        for scope_key in ("scope1_activities", "scope2_activities", "scope3_activities"):
            normalised = _normalise_activities(payload.get(scope_key))
            if normalised is None:
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "invalid_activity_list", "field": scope_key},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [
                        f"InputValidateNode: {scope_key} must be a list of at "
                        f"most {_MAX_ACTIVITIES_PER_SCOPE} records"
                    ],
                }
            scopes[scope_key] = normalised

        if not any(scopes.values()):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "no_emissions_activities"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: at least one of scope1/2/3 activities is required"],
            }

        # ── Per-record contract: inert tokens + finite/bounded numerics ───────
        for scope_key, activities in scopes.items():
            record_error = _validate_scope_activities(activities, scope_key)
            if record_error:
                logger.error("InputValidateNode: invalid activity data — %s", record_error)
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "invalid_activity_data", "detail": record_error},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [f"InputValidateNode: invalid activity data — {record_error}"],
                }

        # ── previous_year_emissions_tco2: finite + bounded ────────────────────
        previous_raw = payload.get("previous_year_emissions_tco2")
        previous_year: Optional[float]
        if previous_raw is None or (isinstance(previous_raw, str) and not previous_raw.strip()):
            previous_year = None
        else:
            ok, previous_year = _finite_in_range(previous_raw, _MAX_EMISSIONS_TCO2)
            if not ok:
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "invalid_previous_year_emissions"},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [
                        "InputValidateNode: previous_year_emissions_tco2 must be "
                        "a finite, non-negative number within bounds"
                    ],
                }

        # ── reduction_commitments: capped list of bounded single-line text ────
        commitments_raw = payload.get("reduction_commitments", [])
        if commitments_raw is None:
            commitments_raw = []
        if not isinstance(commitments_raw, list) or len(commitments_raw) > _MAX_REDUCTION_COMMITMENTS:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "invalid_reduction_commitments"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "InputValidateNode: reduction_commitments must be a list of "
                    f"at most {_MAX_REDUCTION_COMMITMENTS} entries"
                ],
            }
        commitments: List[str] = []
        for idx, entry in enumerate(commitments_raw):
            bounded = _bounded_line(entry, _MAX_COMMITMENT_LEN)
            if bounded is None or not bounded.strip():
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "invalid_reduction_commitment", "index": idx},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [
                        f"InputValidateNode: reduction_commitments[{idx}] must be "
                        f"a single-line string of at most {_MAX_COMMITMENT_LEN} characters"
                    ],
                }
            commitments.append(bounded.strip())

        # ── Build normalised emissions_data ───────────────────────────────────
        emissions_data: Dict[str, Any] = {
            "facility_id": facility_id,
            "company_name": company_name,
            "reporting_year": reporting_year,
            "scope1_activities": scopes["scope1_activities"],
            "scope2_activities": scopes["scope2_activities"],
            "scope3_activities": scopes["scope3_activities"],
            "previous_year_emissions_tco2": previous_year,
            "reduction_commitments": commitments,
        }

        logger.info(
            "InputValidateNode: facility_id=%s year=%d scope1=%d scope2=%d scope3=%d",
            facility_id,
            reporting_year,
            len(scopes["scope1_activities"]),
            len(scopes["scope2_activities"]),
            len(scopes["scope3_activities"]),
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "facility_id": facility_id,
                "reporting_year": reporting_year,
                "scope1_count": len(scopes["scope1_activities"]),
                "scope2_count": len(scopes["scope2_activities"]),
                "scope3_count": len(scopes["scope3_activities"]),
            },
            state,
        )

        return {
            "emissions_data": to_json(emissions_data),
            "status": AgentStatus.SUCCESS.value,
        }
