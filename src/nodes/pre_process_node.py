"""AgentCore Platform v1.0"""

# ENE-C2-007 — PreProcessNode
# Outer backbone pre_process slot (trust gate + emissions input validation).
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level — the framework
#     denies lower-trust callers before execute() runs)
#   - Cap the raw input size (fail-fast before any parsing)
#   - Reject empty / non-JSON input early (fail-fast)
#   - Confirm required emissions fields are present
#   - Validate the request-metadata channel from input_context (inert
#     identifier or "unknown" — metadata degrades, it never rejects)
#   - Write validated_input (normalised JSON string) + enriched_context to State
#   - Emit an audit event for every validation decision
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import re
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# Required top-level keys for a valid emissions payload.
# facility_id is the mandatory identifier; reporting_year anchors the disclosure
# period; scope1_activities is the minimum activity data needed to compute a
# GX-Act / 温対法 compliant emissions report.
_REQUIRED_EMISSIONS_KEYS = frozenset(
    {
        "facility_id",
        "reporting_year",
        "scope1_activities",
    }
)

# Structural cap on the raw request body (matches the adapter-level cap in
# src/api/server.py): fail fast before JSON parsing.
_MAX_INPUT_BYTES = 262_144  # 256 KiB

# Request-metadata channel: inert identifier or "unknown". The channel is
# caller-supplied metadata that lands in enriched_context — free text there
# would be caller-controlled content in an internal record, so anything not
# matching the inert pattern degrades to "unknown" (metadata never rejects).
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class PreProcessNode(FunctionNode):
    """Input validation for ENE-C2-007.

    Validates the caller-supplied emissions payload before the domain
    workflow runs.  This is the outer backbone's pre_process slot — the
    only node with VERIFIED_EXTERNAL trust so that unauthenticated or
    anonymous callers are rejected here (fail-fast; inner domain nodes
    carry ANONYMOUS trust and never see untrusted input directly).

    Input state keys:
        user_input:    str   — caller-supplied JSON emissions payload
        input_context: dict  — caller request metadata (channel)

    Output state keys (partial dict):
        validated_input:  str        — normalised JSON string (re-serialised)
        enriched_context: str        — JSON-serialised channel metadata
        status:           str        — AgentStatus.SUCCESS.value or ERROR
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})
        if not isinstance(input_context, dict):
            input_context = {}

        # ── Size cap (before any parsing) ─────────────────────────────────────
        if isinstance(user_input, str) and len(user_input.encode("utf-8", "ignore")) > _MAX_INPUT_BYTES:
            logger.warning("PreProcessNode: user_input exceeds the size cap")
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "input_too_large"},
                state,
            )
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: user_input exceeds {_MAX_INPUT_BYTES} bytes"],
            }

        # ── Emptiness check ───────────────────────────────────────────────────
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            logger.warning("PreProcessNode: user_input is empty or missing")
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "empty_input"},
                state,
            )
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── JSON parse ────────────────────────────────────────────────────────
        try:
            payload: Dict[str, Any] = json.loads(user_input.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("PreProcessNode: JSON parse failed — %s", exc)
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "json_parse_error", "detail": str(exc)},
                state,
            )
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: invalid JSON — {exc}"],
            }

        if not isinstance(payload, dict):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "payload_not_object"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: JSON root must be an object"],
            }

        # ── Required field check ──────────────────────────────────────────────
        missing = _REQUIRED_EMISSIONS_KEYS - payload.keys()
        if missing:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "missing_required_fields", "missing": sorted(missing)},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: missing required fields: {sorted(missing)}"],
            }

        # ── Success ───────────────────────────────────────────────────────────
        facility_id = str(payload.get("facility_id", "unknown"))
        channel_raw = input_context.get("channel", "")
        channel = channel_raw if isinstance(channel_raw, str) and _CHANNEL_RE.match(channel_raw) else "unknown"
        normalised_json = json.dumps(payload, ensure_ascii=False)

        logger.info(
            "PreProcessNode: validated facility_id=%s payload_keys=%d",
            facility_id,
            len(payload),
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "facility_id": facility_id,
                "payload_keys": sorted(payload.keys()),
            },
            state,
        )

        return {
            "validated_input": normalised_json,
            "enriched_context": to_json(
                {
                    "source": "EmissionsComplianceReportGeneratorAgent",
                    "channel": channel,
                    "facility_id": facility_id,
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }
