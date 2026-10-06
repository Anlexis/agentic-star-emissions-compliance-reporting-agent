"""AgentCore Platform v1.0"""

# ENE-C2-007 — PostProcessNode
# Outer backbone post_process slot: the external-output boundary for the
# emissions compliance report. Three independent layers, in order:
#
#   (1) credential scan — API keys, JWTs, Bearer tokens, password assignments
#       anywhere in ANY outgoing representation (report text, section texts,
#       structured determination) withhold the report entirely (content-free
#       withholding notice, status=ERROR). Recognition is the UNION of the
#       domain patterns and the framework's own detect_credentials(): the
#       framework scans this node's return with that same detector and RAISES
#       on a hit, and a raise discards the node's whole return — the clearing
#       below included — so a shape the framework catches and this gate misses
#       would be a containment bypass, not merely a narrower gate. Blocking
#       CLEARS every caller-facing field (_CLEARED_ON_VIOLATION), not just the
#       status: the envelope resolves `formatted_output or result` with no
#       regard for status;
#   (2) verbatim caller-text redaction — the report is assembled from computed
#       aggregates and validated fields only, so a verbatim embedding of the
#       caller's raw payload (or its normalised form) is a leak, not a
#       feature: any such embedding is replaced with [REDACTED];
#   (3) emissions precision grid — the report schema expresses figures in
#       units of 1,000 tCO2e; every numeric-form token in every outgoing
#       representation is snapped onto that grid (off-grid values are
#       full-precision figures leaking to the external surface), with an
#       audit event per redaction.
#
# Structural identifiers and the grid: the report renders the facility
# identifier verbatim, and such identifiers carry embedded digit runs
# ("ENE-FAC-20260712-001"). Those digits are structure, not a figure, so the
# exact identifier is masked out for the duration of the grid scan and
# restored afterwards. The exemption is deliberately narrow and cannot become
# a bypass: it applies to ONE exact string, taken from the upstream-validated
# request metadata, and only when that string contains a letter — so a
# caller cannot pass an all-digit identifier to exempt a computed figure, and
# no digit run outside that literal token is ever skipped.
#
# The domain output gate is the module-level function `_security_gate_output`
# called from inside execute() — NOT an instance method on the node class
# (the framework auto-wraps node instance methods on the real invoke path,
# which would raise AttributeError).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.nodes.precision_grid import GRID_UNIT_TCO2E

logger = logging.getLogger(__name__)

# Domain credential patterns. These are ADDITIONS to the framework's own
# recognizer (see _security_gate_output) — never a replacement for it. Each one
# is broader than, or absent from, framework.security.credential_detector:
# "pk-"/"ak-" prefixes and 16-char keys (the framework's sk- rule starts at 20),
# a shorter Bearer token, and the assignment form ("api_key = ...") the
# framework does not model at all. Narrowing this list is safe; narrowing the
# UNION is not — see the note on the framework detector below.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
]

# Caller-facing withholding notice written over the output when the gate blocks.
#
# Deliberately NON-EMPTY. AgentBaseGraph.get_output() resolves the caller-facing
# value as `formatted_output or result` with no regard for status, so a falsy
# replacement ("" or {}) does not suppress that fallback — it ACTIVATES it, and
# ships the pre-gate report the gate had just refused. It also carries no
# caller content and no detector name: the fact of the refusal only.
_WITHHELD_NOTICE = (
    "[EMISSIONS REPORT REDACTED: the assembled output did not pass the output gate. "
    "Contact the ESG compliance team for the original report.]"
)

# CONTAINMENT INVENTORY — every caller-facing state field that a blocked
# response must not release, with the value written over it.
#
# Blocking is not a status flip. The envelope resolves `formatted_output or
# result` without consulting status, and EmissionsComplianceReportGeneratorAgent
# .get_output() surfaces the domain fields from state — so returning ERROR while
# leaving those fields populated still hands back the refused report. Every key
# the envelope can surface is listed here; the keys deliberately NOT listed are
# framework-managed and content-free (status, trace_id, correlation_id,
# node_history, execution_time). tests/unit/test_output_envelope.py cross-checks
# this inventory against the envelope itself, so a domain field added to
# get_output() in future cannot quietly stay out of the clearing.
_CLEARED_ON_VIOLATION: Dict[str, Any] = {
    # Truthy on purpose — see _WITHHELD_NOTICE.
    "formatted_output": _WITHHELD_NOTICE,
    # The PRE-gate assembled report and its structured companions.
    "result": None,
    "compliance_report": None,
    "report_sections": None,
    "compliance_flags": None,
    "reporting_required": None,
}

# State fields that must NEVER be embedded verbatim in the external report.
# The report is assembled from computed aggregates and bounded, validated
# fields — the caller's raw JSON payload (or its normalised re-serialisation)
# re-appearing verbatim means unvalidated caller content reached the external
# surface.
_BLOCKED_FIELDS = frozenset(
    {
        "user_input",
        "validated_input",
        "enriched_context",
    }
)

# Approved external precision: emissions figures are expressed in units of
# 1,000 (must match the schema note rendered by
# src/nodes/output_format_node.py — the report RENDERS on this grid, this
# gate ENFORCES it).
_EXTERNAL_ROUND_UNIT = GRID_UNIT_TCO2E
# EXPLICIT output schema — numeric figures are identified by FORM and by
# UNIT-MARKER CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs
#            of 5+ digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency-style
#            marker is a figure even though short — SYMMETRICALLY: a 3-letter
#            uppercase code or a currency symbol (incl. fullwidth ￥ and
#            円/₩), before or after the value, attached or separated by ANY
#            whitespace run (spaces, tabs, newlines — the delimiter grammar
#            is `\s*`), signed or unsigned. Word-boundary guards keep
#            embedded acronyms structural ("STAR 2026" does not match — the
#            3 letters must be a standalone word). Any standalone 3-letter
#            uppercase word counts as a marker on purpose: a false snap fails
#            SAFE while a missed leak does not.
# Structural tokens stay untouched: horizon tags ("90d"), version tags
# ("v12"), bare counts, years without marker adjacency ("in 2026"), and the
# lowercase-prefixed unit "tCO2e".
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision figure reaching the external surface,
# snapped + audited.
# Grammar (group-based; no lookbehinds, so the delimiter can be ARBITRARY
# whitespace — spaces, tabs, newlines, any run length). Every value accepts
# an optional explicit +/- sign.
# Branch order matters: marker-context branches first, then form-based.
_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"
# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A figure may carry a DECIMAL part, and every value alternative absorbs it as
# part of the same token. Without that, the fraction of "9999.99999" is a
# standalone five-digit run in its own right: nothing stopped a match from
# beginning there (a decimal point is neither a digit nor an identifier
# character, so neither the word boundaries nor the structural-identifier mask
# excluded it), and the snap rewrote a percentage into "9999.100,000". The same
# defect split an off-grid amount in two — "JPY 1234.56" had its integer part
# snapped while the fraction dangled, yielding "JPY 1,000.56": neither the true
# figure nor a figure on the grid.
# Absorbing the fraction with a bare optional re-opens that split from the
# other side: `(?:\.\d+)?` lets the engine backtrack out of the fraction when
# the character behind it fails the trailing guard and re-match the integer
# part alone, so suffixed text ("JPY 1234.56m") still came back split as
# "JPY 1,000.56m". The two-arm form is not optional about it: the fraction is
# consumed whole, or the grammar asserts none begins at this position.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    # LEADING guard: a match may not BEGIN inside a number — neither part-way
    # through a digit run nor inside a fraction. It covers digits and the
    # decimal point ONLY, deliberately: widening it to letters, "_" or "-"
    # would exempt every digit run that happens to sit next to one, i.e.
    # reinstate a FORM-based exemption. Structural identifiers keep their
    # exemption the way this gate has always granted it — by being declared
    # and MASKED (see _mask_structural_ids), never by their shape.
    # There is deliberately NO trailing guard: an amount that closes a sentence
    # ("The book totals JPY 9999.") must still be snapped.
    r"(?<![0-9.])"
    r"(?:"
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # — an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    rf"(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})\b"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})"
    rf"(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION})"
    r")"
)


# Sentinels used to mask structural identifiers during the grid scan. Both are
# private-use code points: they are not word characters, carry no digits, and
# cannot occur in a rendered report.
_MASK_OPEN = "\ue000"
_MASK_CLOSE = "\ue001"
_LETTERS = "abcdefghijklmnopqrstuvwxyz"

# A structural identifier may be exempted from the grid scan only if it
# contains at least one letter — an all-digit token is a figure, not
# structure, and must stay subject to the grid.
_STRUCTURAL_ID_RE = re.compile(r"^(?=[A-Za-z0-9_-]{1,64}$)[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*$")


def _mask_index(index: int) -> str:
    """Render a mask index using letters only (never digits)."""
    label = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        label = _LETTERS[rem] + label
    return label


def _mask_structural_ids(text: str, identifiers: Tuple[str, ...]) -> tuple[str, Dict[str, str]]:
    """Replace exact structural identifiers with digit-free placeholders."""
    mapping: Dict[str, str] = {}
    # Longest first so a shorter identifier cannot partially consume a longer one.
    for index, token in enumerate(sorted(set(identifiers), key=len, reverse=True)):
        if not _STRUCTURAL_ID_RE.match(token) or token not in text:
            continue
        placeholder = f"{_MASK_OPEN}{_mask_index(index)}{_MASK_CLOSE}"
        mapping[placeholder] = token
        text = text.replace(token, placeholder)
    return text, mapping


def _unmask_structural_ids(text: str, mapping: Dict[str, str]) -> str:
    """Restore masked structural identifiers."""
    for placeholder, token in mapping.items():
        text = text.replace(placeholder, token)
    return text


def _enforce_precision(result: str, identifiers: Tuple[str, ...] = ()) -> tuple[str, int]:
    """Snap every numeric-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision figure reached the external surface — the gate rounds it
    onto the approved grid. The unit marker, the original delimiter
    whitespace, and the explicit sign of the original token are all preserved
    on the snapped replacement.

    `identifiers` lists exact structural identifier strings (validated
    upstream) whose embedded digit runs are structure, not figures; they are
    masked for the duration of the scan and restored afterwards.
    """
    redactions = 0
    result, mask_map = _mask_structural_ids(result, identifiers)

    def _snap(match: re.Match[str]) -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): a token may now carry a decimal part, and the
        # whole amount — fraction included — is what has to land on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    scanned = _NUM_TOKEN_RE.sub(_snap, result)
    return _unmask_structural_ids(scanned, mask_map), redactions


def _security_gate_output(content: str) -> Optional[str]:
    """Scan output for disallowed credential/secret patterns.

    Returns the violation CLASS name (never the matched value), or None if the
    output is clean. Module-level function (not a node instance method) — the
    framework auto-wraps node instance methods on the real invoke path, so the
    gate must live at module level.

    The scan is the UNION of the domain patterns above and the framework's own
    ``detect_credentials()``. Delegating to the framework recognizer is not
    belt-and-braces, it is required for containment: FunctionNode's @final
    output gate scans every value this node returns with that same detector
    and RAISES on a hit, and BaseNode.__call__ then replaces the node's whole
    return with a bare error partial — discarding the clearing in
    _CLEARED_ON_VIOLATION along with it, and leaving the pre-gate report in
    state. A shape the framework catches and this gate misses is therefore a
    containment BYPASS, not merely a narrower gate. Four shapes the framework
    refuses had no domain equivalent here: ``sk_live_``/``sk_test_`` (underscore
    form), ``AKIA…``, a dotless ``eyJ…`` JWT, and ``postgresql://``-style
    connection strings.
    """
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    findings = detect_credentials(content)
    if findings:
        # The finding's "type" is a detector class ("aws_key"), not the value.
        return str(findings[0]["type"])
    return None


def _gate_violations(representations: "List[Tuple[str, str]]") -> List[str]:
    """Run the output gate over each named outgoing representation.

    Returns one message per offending representation, naming the LOCATION and
    the detector class — never the matched value. Echoing the value would put
    the refused string back into this node's own return, where the framework's own
    credential scan raises and discards the containment (see _security_gate_output).
    """
    violations: List[str] = []
    for location, content in representations:
        if not content:
            continue
        name = _security_gate_output(content)
        if name:
            violations.append(f"PostProcessNode: credential pattern detected in output[{location}] — {name}")
    return violations


def _redact_blocked_fields(result: str, state: AgentState) -> tuple[str, List[str]]:
    """Replace verbatim embeddings of caller-derived state text with [REDACTED].

    Returns (sanitised_result, redacted_field_names). Only substantial values
    (len > 10) are matched so short incidental overlaps are not redacted.
    """
    redacted: List[str] = []
    sanitised = result
    for field in sorted(_BLOCKED_FIELDS):
        value = state.get(field)
        if isinstance(value, str) and len(value) > 10 and value in sanitised:
            sanitised = sanitised.replace(value, "[REDACTED]")
            redacted.append(field)
    return sanitised, redacted


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Apply the output gate to every outgoing representation of the report.

    Outer backbone post_process slot.  Declared ANONYMOUS — trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    The gated representations are compliance_report/result (the assembled
    report text), report_sections (the per-section texts, surfaced as a JSON
    string), and compliance_flags (the structured determination, surfaced as
    a JSON string) — everything the invoke() return exposes to the caller.

    Input state keys:
        compliance_report:  str  — formatted report from inner OutputFormatNode
        report_sections:    str  — JSON-serialised section dict
        compliance_flags:   str  — JSON-serialised determination dict
        reporting_required: bool

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        report_sections:  str  (gated re-serialisation)
        compliance_flags: str  (gated re-serialisation)
        status:           str
        error_log:        list[str]  (only on ERROR)

    On a gate violation the return is _CLEARED_ON_VIOLATION + status/error_log:
    every caller-facing field is overwritten, formatted_output with a truthy
    withholding notice (a falsy one would ACTIVATE the envelope's
    `formatted_output or result` fallback rather than suppress it).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
            }
        compliance_report: str = state.get("compliance_report") or ""
        reporting_required: bool = bool(state.get("reporting_required"))
        sections_json: str = state.get("report_sections") or ""
        flags_json: str = state.get("compliance_flags") or ""
        identifiers = _structural_identifiers(state)

        # ── Fallback for empty report ─────────────────────────────────────────
        if not compliance_report.strip():
            logger.warning("PostProcessNode: compliance_report is empty — using fallback message")
            compliance_report = (
                "[Emissions Compliance Report] No report content generated. " "Check error_log for upstream failures."
            )

        # ── Layer 1: credential scan across EVERY representation ──────────────
        violations = _gate_violations(
            [
                ("compliance_report", compliance_report),
                ("report_sections", sections_json),
                ("compliance_flags", flags_json),
            ]
        )
        if violations:
            logger.error("PostProcessNode: output gate blocked the response — %d violation(s)", len(violations))
            emit_trace_event(
                "post_process_credential_violation",
                {"violation_count": len(violations)},
                state,
            )
            # CONTAINMENT — blocking is not a status flip.
            #
            # AgentBaseGraph.get_output() resolves the caller-facing value as
            # `formatted_output or result` WITHOUT consulting status, and this
            # agent's own get_output() surfaces the domain fields from state.
            # Returning ERROR while leaving those fields populated therefore
            # ships the very report this gate refused, inside an error
            # envelope. Every caller-facing field is overwritten here from the
            # _CLEARED_ON_VIOLATION inventory, and the formatted_output
            # replacement is deliberately non-empty so the `or result`
            # fallback cannot re-open the path this branch just closed.
            #
            # The messages name LOCATIONS and detector classes only. Echoing
            # the offending value would put it back into this node's own
            # return, where the framework's @final output scan raises — and a
            # raise makes BaseNode.__call__ discard this whole dict, clearing
            # included, leaving the pre-gate report in state.
            return {
                **_CLEARED_ON_VIOLATION,
                "status": AgentStatus.ERROR.value,
                "error_log": violations,
            }

        # ── Layer 2: verbatim caller-text redaction ───────────────────────────
        sanitised_report, redacted_fields = _redact_blocked_fields(compliance_report, state)
        if redacted_fields:
            logger.error(
                "PostProcessNode: caller-derived text embedded verbatim in output — %s",
                ", ".join(redacted_fields),
            )
            emit_trace_event(
                "post_process_blocked_field_redaction",
                {"fields": redacted_fields},
                state,
            )

        # ── Layer 3: precision grid on every representation ───────────────────
        total_redactions = 0
        sanitised_report, n = _enforce_precision(sanitised_report, identifiers)
        total_redactions += n

        gated_sections_json = sections_json
        sections = _load_json_dict(sections_json)
        if sections is not None:
            gated_sections: Dict[str, Any] = {}
            for key, text in sections.items():
                if isinstance(text, str):
                    text, n = _enforce_precision(text, identifiers)
                    total_redactions += n
                gated_sections[key] = text
            gated_sections_json = json.dumps(gated_sections, ensure_ascii=False)

        gated_flags_json = flags_json
        flags = _load_json_dict(flags_json)
        if flags is not None:
            reason = flags.get("reason")
            if isinstance(reason, str):
                flags["reason"], n = _enforce_precision(reason, identifiers)
                total_redactions += n
            total_grid = flags.get("total_tco2_nearest_1000")
            if isinstance(total_grid, (int, float)) and not isinstance(total_grid, bool):
                if int(total_grid) % _EXTERNAL_ROUND_UNIT != 0:
                    flags["total_tco2_nearest_1000"] = int(
                        round(float(total_grid) / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
                    )
                    total_redactions += 1
            gated_flags_json = json.dumps(flags, ensure_ascii=False)

        if total_redactions:
            logger.warning(
                "PostProcessNode: %d off-grid figure(s) snapped to the external grid",
                total_redactions,
            )
            emit_trace_event(
                "post_process_precision_redaction",
                {"redaction_count": total_redactions},
                state,
            )

        logger.info(
            "PostProcessNode: output gate passed — length=%d reporting_required=%s",
            len(sanitised_report),
            reporting_required,
        )
        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(sanitised_report),
                "reporting_required": reporting_required,
            },
            state,
        )

        return {
            "formatted_output": sanitised_report,
            "result": sanitised_report,
            "compliance_report": sanitised_report,
            "report_sections": gated_sections_json or None,
            "compliance_flags": gated_flags_json or None,
            "status": AgentStatus.SUCCESS.value,
        }


def _structural_identifiers(state: AgentState) -> Tuple[str, ...]:
    """Collect the exact structural identifiers rendered into the report.

    Sourced from enriched_context — the request metadata PreProcessNode wrote
    after validating the payload, so the value is an inert identifier
    (``[A-Za-z0-9_-]{1,64}``) rather than free caller text. Anything absent or
    malformed yields no exemption at all (fail closed: the grid then applies
    to the whole document).
    """
    context = _load_json_dict(state.get("enriched_context") or "")
    if not context:
        return ()
    facility_id = context.get("facility_id")
    if not isinstance(facility_id, str):
        return ()
    return (facility_id,)


def _load_json_dict(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object string; return None when absent or not an object."""
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None
