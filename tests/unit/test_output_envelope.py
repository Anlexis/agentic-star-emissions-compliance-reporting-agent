# ENE-C2-007 — Unit tests: the outer invoke() envelope
# (EmissionsComplianceReportGeneratorAgent.get_output)
#
# The envelope is the last thing between graph state and the caller, and the
# second half of the output-gate contract. The framework base resolves its
# `output` key as `formatted_output or result` WITHOUT consulting status, and
# `result` is the PRE-gate value the workflow produced — so an envelope that
# forwards state verbatim hands back the answer the gate refused, inside an
# error envelope. Worse, a template that RE-IMPLEMENTS that expression defeats
# even a future framework-side status guard.
#
# These call get_output() directly on a state dict so the resolution rule is
# pinned independently of which node happened to produce that state; the
# end-to-end consequence is pinned on the real /invoke surface in
# tests/proof_of_boundary/test_output_gate_containment.py.

import inspect
import json

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import EmissionsComplianceReportGeneratorAgent, EmissionsReportGraphNode
from src.nodes.post_process_node import _CLEARED_ON_VIOLATION

# What the workflow produced before the output gate ran.
_PRE_GATE_REPORT = "EMISSIONS COMPLIANCE REPORT\nTokyo Bay Thermal — Total Emissions: 135,000 tCO2e"

# Envelope keys owned by the framework base: content-free routing/trace values
# plus `output`, which is DERIVED from formatted_output rather than read from a
# domain state field.
_FRAMEWORK_ENVELOPE_KEYS = frozenset({"output", "status", "trace_id", "correlation_id", "node_history"})


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "formatted_output": "GATED REPORT TEXT",
        "result": _PRE_GATE_REPORT,
        "compliance_report": _PRE_GATE_REPORT,
        "report_sections": json.dumps({"emissions_summary": _PRE_GATE_REPORT}),
        "compliance_flags": json.dumps({"reporting_required": True}),
        "reporting_required": True,
        "trace_id": "envelope-test",
        "correlation_id": "envelope-test",
        "node_history": ["PostProcessNode"],
    }
    state.update(overrides)
    return state


def _envelope(**overrides) -> dict:
    return EmissionsComplianceReportGeneratorAgent().get_output(_state(**overrides))


class TestSuccessEnvelope:
    """The control. Without it every containment assertion below would also
    pass on an envelope that returns nothing at all."""

    def test_success_surfaces_the_gated_result(self):
        envelope = _envelope()
        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert envelope["output"] == "GATED REPORT TEXT"
        assert envelope["formatted_output"] == "GATED REPORT TEXT"
        # compliance_report is the POST-gate value, never the raw state field.
        assert envelope["compliance_report"] == "GATED REPORT TEXT"
        assert envelope["result"] == _PRE_GATE_REPORT
        assert envelope["reporting_required"] is True
        assert json.loads(envelope["report_sections"])["emissions_summary"]
        assert json.loads(envelope["compliance_flags"])["reporting_required"] is True
        for key in ("trace_id", "correlation_id", "node_history"):
            assert key in envelope


class TestErrorEnvelopeContainment:
    def test_pre_gate_result_is_never_surfaced_on_an_error(self):
        envelope = _envelope(status=AgentStatus.ERROR.value)
        assert envelope["result"] is None
        assert _PRE_GATE_REPORT not in json.dumps(envelope)

    def test_the_or_result_fallback_is_dead_on_an_error(self):
        """The hole in the base envelope: with no formatted_output, `output`
        falls through to the pre-gate `result`. On a non-success outcome an
        absent gate output must stay absent, never become the inner answer.
        This is the only containment on the paths where the framework itself
        refuses — a bare ERROR partial clears nothing, so the gate's own
        clearing never ran."""
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=None)
        assert not envelope["output"]
        assert "135,000" not in json.dumps(envelope)

    def test_error_surfaces_the_gate_output_and_nothing_else(self):
        withheld = _CLEARED_ON_VIOLATION["formatted_output"]
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=withheld)
        assert envelope["output"] == withheld
        assert envelope["formatted_output"] == withheld
        assert envelope["result"] is None

    def test_structured_keys_are_withheld_on_an_error(self):
        envelope = _envelope(status=AgentStatus.ERROR.value)
        for key in ("compliance_report", "report_sections", "compliance_flags", "reporting_required"):
            assert envelope[key] is None

    def test_containment_holds_for_every_non_success_status(self):
        """Not an ERROR special case: any status that is not SUCCESS means the
        output gate did not pass the response — including the terminal statuses
        that route straight to finalize without post_process running at all."""
        for status in (
            AgentStatus.TIMEOUT.value,
            AgentStatus.CANCELLED.value,
            AgentStatus.RETRY.value,
            AgentStatus.PENDING.value,
        ):
            envelope = _envelope(status=status, formatted_output=None)
            assert envelope["result"] is None, status
            assert not envelope["output"], status
            assert envelope["compliance_report"] is None, status


class TestEnvelopeDoesNotReproduceTheFrameworkFallback:
    """`formatted_output or result` must exist in exactly one place.

    A template that repeats the framework's own un-guarded resolution defeats a
    framework-side status guard before it can help: the base would withhold and
    the override would put the value straight back.
    """

    def test_get_output_does_not_re_implement_the_fallback(self):
        source = inspect.getsource(EmissionsComplianceReportGeneratorAgent.get_output)
        body = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        body = body.split('"""')[-1]  # drop the docstring, which discusses the expression
        assert 'state.get("formatted_output") or state.get("result")' not in body
        assert 'formatted_output or state.get("result")' not in body


class TestContainmentInventoryMatchesTheEnvelope:
    """Inventory guard — a domain field added to the envelope later cannot
    quietly stay out of the gate's clearing."""

    def test_every_domain_key_the_envelope_surfaces_is_cleared_on_a_block(self):
        surfaced = set(_envelope().keys()) - _FRAMEWORK_ENVELOPE_KEYS
        assert surfaced == set(_CLEARED_ON_VIOLATION), (
            "the envelope surfaces a domain field the output gate does not clear "
            f"(envelope-only: {sorted(surfaced - set(_CLEARED_ON_VIOLATION))}, "
            f"inventory-only: {sorted(set(_CLEARED_ON_VIOLATION) - surfaced)})"
        )

    def test_the_replacement_defeats_the_envelope_fallback(self):
        """A falsy replacement ("" or {}) hands the resolution straight back to
        `result` — the exact hole the clearing closes."""
        assert _CLEARED_ON_VIOLATION["formatted_output"]
        assert all(value is None for key, value in _CLEARED_ON_VIOLATION.items() if key != "formatted_output")

    def test_merge_output_does_not_write_the_gate_owned_fields(self):
        """`formatted_output` is surfaced on BOTH paths because the output gate
        is its only writer. That is a coupling, so it is pinned: the inner
        graph's result must not arrive pre-labelled as caller-facing output."""
        node = EmissionsReportGraphNode(config={})
        merged = node.merge_output(
            {},
            {
                "compliance_report": _PRE_GATE_REPORT,
                "report_sections": None,
                "compliance_flags": None,
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": _PRE_GATE_REPORT,
                "result": _PRE_GATE_REPORT,
            },
        )
        assert "formatted_output" not in merged
        assert "result" not in merged
