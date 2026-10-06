# ENE-C2-007 — Integration regression
#
# The compiled OUTER graph's success path must return the domain emissions
# compliance result to the caller.
#
# Behaviour locked in: EmissionsComplianceReportGeneratorAgent would otherwise
# inherit AgentBaseGraph.get_output(), which surfaces only the
# {output, status, ...} envelope — a successful agent.invoke() would drop the
# structured domain result (formatted_output / result / compliance_report /
# report_sections / compliance_flags / reporting_required) even though
# PostProcessNode and the inner DomainWorkflowGraph populate them on the
# internal state and EmissionsReportGraphNode.merge_output() merges them up.
# An ["output"]-only backbone test would never exercise this.
#
# This test invokes the COMPILED OUTER graph (Graph().compile().invoke(...)) —
# the same construction the PB-6 backbone test uses — with a valid
# VERIFIED_EXTERNAL emissions payload, and asserts the domain result is
# surfaced. A second case proves the output gate is NOT weakened: a
# credential in a caller field blocks the invoke and the structured fields are
# withheld (fail-closed).

import json

from framework.schemas.agent_status import AgentStatus


# A valid VERIFIED_EXTERNAL emissions payload (mirrors the PB-6 _VALID_PAYLOAD):
# all PreProcessNode + InputValidateNode required fields present, all numerics
# finite + non-negative, total >> 3,000 tCO2e so reporting_required is True, and
# no credential / secret patterns so the output gate passes cleanly.
_VALID_PAYLOAD = json.dumps(
    {
        "facility_id": "ENE-FAC-20260714-001",
        "company_name": "Tokyo Bay Thermal Power K.K.",
        "reporting_year": 2025,
        "scope1_activities": [
            {"fuel_type": "coal", "quantity": 50000},
            {"fuel_type": "city_gas", "quantity": 1200000},
        ],
        "scope2_activities": [
            {"energy_type": "grid_electricity", "quantity": 8000000},
        ],
        "scope3_activities": [
            {"category": "purchased_goods_and_services", "emissions_tco2": 15000},
        ],
        "previous_year_emissions_tco2": 145000,
        "reduction_commitments": [
            "Reduce Scope 1+2 emissions 46% by FY2030 versus the FY2013 baseline.",
            "Achieve carbon neutrality across all scopes by FY2050.",
        ],
    }
)

# Same shape, but company_name carries a credential secret. company_name is
# echoed verbatim into the assembled compliance_report header, so the output
# gate must block it: status -> ERROR, sanitised stub, and the structured
# domain fields withheld.
_CREDENTIAL_PAYLOAD = json.dumps(
    {
        "facility_id": "ENE-FAC-20260714-002",
        "company_name": "Acme Energy sk-abcdefghij0123456789ABCDEF K.K.",
        "reporting_year": 2025,
        "scope1_activities": [
            {"fuel_type": "coal", "quantity": 50000},
        ],
        "scope2_activities": [
            {"energy_type": "grid_electricity", "quantity": 8000000},
        ],
        "scope3_activities": [
            {"category": "purchased_goods_and_services", "emissions_tco2": 15000},
        ],
        "previous_year_emissions_tco2": 145000,
        "reduction_commitments": [],
    }
)

_SECRET = "sk-abcdefghij0123456789ABCDEF"


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "parse_emissions_data_node",
        "generate_report_sections_node",
        "compliance_check_node",
        "output_format_node",
        "post_process_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not imported / no emit symbol; fine


def _invoke(monkeypatch, payload):
    """Compile and invoke the OUTER graph as a real VERIFIED_EXTERNAL caller."""
    _patch_domain_emit(monkeypatch)
    from framework.schemas.invocation_context import InvocationContext, TrustLevel
    from src.graph.graph import Graph

    agent = Graph()
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(payload, ctx=ctx)


class TestOuterInvokeReturnsDomainResult:
    """A successful outer invoke must surface the domain result."""

    def test_success_invoke_surfaces_domain_result(self, monkeypatch):
        result = _invoke(monkeypatch, _VALID_PAYLOAD)

        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"expected SUCCESS, got {result.get('status')!r}; " f"error_log={result.get('error_log')}"
        )

        # The regression: these were ALL None before the get_output() override.
        formatted_output = result.get("formatted_output")
        compliance_report = result.get("compliance_report")
        assert formatted_output is not None, "formatted_output must be surfaced on a successful invoke"
        assert compliance_report is not None, "compliance_report must be surfaced on a successful invoke"
        assert result.get("result") is not None, "result must be surfaced on a successful invoke"
        assert result.get("reporting_required") is True, "reporting_required must be surfaced (total >> 3,000 tCO2e)"
        assert result.get("report_sections") is not None, "report_sections must be surfaced on a successful invoke"
        assert result.get("compliance_flags") is not None, "compliance_flags must be surfaced on a successful invoke"

        # The surfaced report must actually be the assembled emissions report.
        for needle in ("EMISSIONS COMPLIANCE REPORT", "ENE-FAC-20260714-001", "REGULATORY COMPLIANCE NOTE"):
            assert needle in formatted_output, f"{needle!r} missing from formatted_output"
            assert needle in compliance_report, f"{needle!r} missing from compliance_report"

        # compliance_flags (structured) is surfaced and carries the determination.
        flags = json.loads(result["compliance_flags"])
        assert flags["reporting_required"] is True
        assert flags["threshold_mandatory_tco2"] == 3000

        # The framework envelope is preserved (backward compatible).
        assert result.get("output") is not None

    def test_credential_block_withholds_structured_fields(self, monkeypatch):
        """Output gate not weakened: a credential in a caller field blocks the
        invoke and the structured domain fields are withheld (fail-closed)."""
        result = _invoke(monkeypatch, _CREDENTIAL_PAYLOAD)

        assert (
            result.get("status") == AgentStatus.ERROR.value
        ), f"expected the output gate to block, got status={result.get('status')!r}"
        # The credential-bearing structured result is NOT surfaced.
        assert result.get("compliance_report") is None
        assert result.get("report_sections") is None
        assert result.get("compliance_flags") is None
        assert result.get("reporting_required") is None
        # The caller-facing output carries only the sanitised stub — never the raw secret.
        assert _SECRET not in (result.get("formatted_output") or "")
        assert _SECRET not in (result.get("result") or "")
        assert _SECRET not in (result.get("output") or "")
