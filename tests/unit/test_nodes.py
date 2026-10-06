# ENE-C2-007 — Unit Tests: domain nodes + graph wiring
#
# Real, non-stub unit tests asserting real behaviour: report content,
# 温対法 / GX-ETS thresholds, emission-factor computation, node trust levels,
# the output gate (credential scan + caller-text redaction + precision grid),
# and the Cat 2 two-layer graph composition.
#
# Audit events are patched at the node MODULE level (not via a sys.modules
# stub, which would break the real `shared` package the framework loads at import
# time). Patch pattern per node:
#     monkeypatch.setattr("src.nodes.<mod>.emit_trace_event", lambda *a, **k: None)

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.schemas.state import from_json, to_json


# ── Shared fixtures / helpers ─────────────────────────────────────────────────


def _emissions_payload(**overrides) -> dict:
    """A complete, valid raw emissions payload (as a caller would POST)."""
    payload = {
        "facility_id": "ENE-FAC-20260712-001",
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
    payload.update(overrides)
    return payload


VALID_PAYLOAD = json.dumps(_emissions_payload())


def _emissions_data(scope1=None, scope2=None, scope3=None, previous=None, **overrides) -> dict:
    """The normalised emissions_data dict shape produced by InputValidateNode
    (i.e. the input the downstream inner nodes consume)."""
    data = {
        "facility_id": "ENE-FAC-20260712-001",
        "company_name": "Tokyo Bay Thermal Power K.K.",
        "reporting_year": 2025,
        "scope1_activities": scope1 if scope1 is not None else [{"fuel_type": "coal", "quantity": 50000}],
        "scope2_activities": scope2
        if scope2 is not None
        else [{"energy_type": "grid_electricity", "quantity": 8000000}],
        "scope3_activities": scope3
        if scope3 is not None
        else [{"category": "purchased_goods_and_services", "emissions_tco2": 15000}],
        "previous_year_emissions_tco2": previous,
        "reduction_commitments": [],
    }
    data.update(overrides)
    return data


def _computed_data(total_tco2: float = 5000.0, previous=145000, reduction_commitments=None, **overrides) -> dict:
    """An emissions_data dict already annotated with computed totals (as
    ParseEmissionsDataNode leaves it for the downstream inner nodes)."""
    data = _emissions_data(previous=previous)
    if reduction_commitments is not None:
        data["reduction_commitments"] = reduction_commitments
    data.update(
        {
            "scope1_tco2": 0.0,
            "scope2_tco2": 0.0,
            "scope3_tco2": total_tco2,
            "total_tco2": total_tco2,
            "emitter_class": "specified_emitter",
            "data_quality_flags": [],
        }
    )
    data.update(overrides)
    return data


# ── PreProcessNode (outer pre_process, VERIFIED_EXTERNAL trust) ────────────────


class TestPreProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.pre_process_node import PreProcessNode

        self.node = PreProcessNode()

    def test_valid_payload_returns_success(self):
        result = self.node(
            {"user_input": VALID_PAYLOAD, "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None
        assert json.loads(result["validated_input"])["facility_id"] == "ENE-FAC-20260712-001"

    def test_enriched_context_carries_facility_id(self):
        result = self.node(
            {
                "user_input": VALID_PAYLOAD,
                "input_context": {"channel": "esg_portal"},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        ctx = from_json(result["enriched_context"])
        assert ctx["facility_id"] == "ENE-FAC-20260712-001"
        assert ctx["channel"] == "esg_portal"
        assert ctx["source"] == "EmissionsComplianceReportGeneratorAgent"

    def test_empty_input_returns_error(self):
        result = self.node(
            {"user_input": "", "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("empty" in e for e in result["error_log"])

    def test_invalid_json_returns_error(self):
        result = self.node(
            {
                "user_input": "{not valid json}",
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("JSON" in e or "json" in e for e in result["error_log"])

    def test_non_object_json_returns_error(self):
        result = self.node(
            {"user_input": "[1, 2, 3]", "input_context": {}, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("object" in e for e in result["error_log"])

    def test_missing_required_field_returns_error(self):
        payload = {"facility_id": "F1", "reporting_year": 2025}  # no 'scope1_activities'
        result = self.node(
            {
                "user_input": json.dumps(payload),
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("scope1_activities" in e for e in result["error_log"])

    def test_trust_level_is_verified_external(self):
        assert self.node.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_execute_signature_is_state_first(self):
        import inspect
        from src.nodes.pre_process_node import PreProcessNode

        params = list(inspect.signature(PreProcessNode.execute).parameters.keys())
        assert params[0] == "self" and params[1] == "state"
        assert "_invoke_impl" not in PreProcessNode.__dict__


# ── InputValidateNode (inner domain node 1, ANONYMOUS) ─────────────────────────


class TestInputValidateNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.input_validate_node import InputValidateNode

        self.node = InputValidateNode()

    def test_valid_input_builds_emissions_data(self):
        result = self.node({"validated_input": VALID_PAYLOAD, "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["emissions_data"])
        assert data["facility_id"] == "ENE-FAC-20260712-001"
        assert data["reporting_year"] == 2025
        assert len(data["scope1_activities"]) == 2

    def test_company_name_defaults_to_facility_id(self):
        payload = _emissions_payload(company_name="")
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        data = from_json(result["emissions_data"])
        assert data["company_name"] == data["facility_id"]

    def test_falls_back_to_user_input(self):
        result = self.node({"user_input": VALID_PAYLOAD, "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_empty_facility_id_returns_error(self):
        payload = _emissions_payload(facility_id="")
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("facility_id" in e for e in result["error_log"])

    def test_reporting_year_out_of_range_returns_error(self):
        payload = _emissions_payload(reporting_year=1990)
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("reporting_year" in e for e in result["error_log"])

    def test_no_activities_returns_error(self):
        payload = _emissions_payload(scope1_activities=[], scope2_activities=[], scope3_activities=[])
        result = self.node({"validated_input": json.dumps(payload), "caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("activities" in e for e in result["error_log"])

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── ParseEmissionsDataNode (inner domain node 2, ANONYMOUS) ────────────────────


class TestParseEmissionsDataNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.parse_emissions_data_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.parse_emissions_data_node import ParseEmissionsDataNode

        self.node = ParseEmissionsDataNode()

    def _run(self, scope1=None, scope2=None, scope3=None):
        state = {
            "emissions_data": to_json(_emissions_data(scope1=scope1, scope2=scope2, scope3=scope3)),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        return self.node(state)

    def test_scope1_uses_environment_ministry_fuel_factor(self):
        # coal factor 2.33 tCO2/t -> 1000 t = 2330.0 tCO2
        data = from_json(
            self._run(scope1=[{"fuel_type": "coal", "quantity": 1000}], scope2=[], scope3=[])["emissions_data"]
        )
        assert data["scope1_tco2"] == 2330.0

    def test_scope2_uses_grid_electricity_factor(self):
        # grid factor 0.000434 tCO2/kWh -> 1,000,000 kWh = 434.0 tCO2
        data = from_json(
            self._run(scope1=[], scope2=[{"energy_type": "grid_electricity", "quantity": 1000000}], scope3=[])[
                "emissions_data"
            ]
        )
        assert data["scope2_tco2"] == 434.0

    def test_scope3_is_summed(self):
        data = from_json(
            self._run(scope1=[], scope2=[], scope3=[{"category": "x", "emissions_tco2": 5000}])["emissions_data"]
        )
        assert data["scope3_tco2"] == 5000.0
        assert data["total_tco2"] == 5000.0

    def test_emitter_class_large_gx_ets(self):
        data = from_json(
            self._run(scope1=[], scope2=[], scope3=[{"category": "x", "emissions_tco2": 150000}])["emissions_data"]
        )
        assert data["emitter_class"] == "large_emitter_gx_ets"

    def test_emitter_class_specified(self):
        data = from_json(
            self._run(scope1=[], scope2=[], scope3=[{"category": "x", "emissions_tco2": 5000}])["emissions_data"]
        )
        assert data["emitter_class"] == "specified_emitter"

    def test_emitter_class_below_threshold(self):
        data = from_json(
            self._run(scope1=[], scope2=[], scope3=[{"category": "x", "emissions_tco2": 100}])["emissions_data"]
        )
        assert data["emitter_class"] == "below_mandatory_threshold"

    def test_unknown_fuel_type_flagged(self):
        data = from_json(
            self._run(
                scope1=[{"fuel_type": "plutonium", "quantity": 100}],
                scope2=[],
                scope3=[{"category": "x", "emissions_tco2": 5000}],
            )["emissions_data"]
        )
        assert any("unknown_scope1_fuel_type" in f for f in data["data_quality_flags"])

    def test_missing_emissions_data_returns_error(self):
        result = self.node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── GenerateReportSectionsNode (inner domain node 3, ANONYMOUS) ────────────────


class TestGenerateReportSectionsNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.generate_report_sections_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.generate_report_sections_node import GenerateReportSectionsNode

        self.node = GenerateReportSectionsNode()

    def test_generates_all_five_sections(self):
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(previous=145000)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        sections = from_json(result["report_sections"])
        assert set(sections.keys()) == {
            "emissions_summary",
            "calculation_methodology",
            "regulatory_framework",
            "year_over_year_comparison",
            "reduction_commitments",
        }

    def test_summary_reflects_reporting_year(self):
        sections = from_json(
            self.node({"emissions_data": to_json(_computed_data()), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "report_sections"
            ]
        )
        assert "2025" in sections["emissions_summary"]
        assert "Total Emissions" in sections["emissions_summary"]

    def test_summary_renders_aggregates_on_the_grid(self):
        # 137,719.512 must render as the 1,000-grid aggregate, never at full precision.
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(_computed_data(total_tco2=137_719.512)),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        summary = sections["emissions_summary"]
        assert "138,000 tCO2e" in summary
        assert "137,719" not in summary
        assert "nearest 1,000" in summary

    def test_methodology_never_renders_caller_factor_values(self):
        # A caller-supplied factor and its provenance text are recorded
        # internally but must not be reproduced on the external surface.
        data = _computed_data(total_tco2=5000.0)
        data["factor_review_required"] = True
        data["supplied_factor_records"] = [
            {
                "energy_type": "on_site_biomass",
                "factor_tco2_per_unit": 0.2,
                "provenance": "CALLER SUPPLIED PROVENANCE TEXT",
                "status": "supplied_pending_review",
            }
        ]
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(data),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        methodology = sections["calculation_methodology"]
        assert "on_site_biomass" in methodology
        assert "supplied_pending_review" in methodology
        assert "CALLER SUPPLIED PROVENANCE TEXT" not in methodology
        assert "0.2" not in methodology

    def test_year_over_year_uses_grid_rounded_figures(self):
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(_computed_data(total_tco2=137_719.512, previous=145_432.1)),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        yoy = sections["year_over_year_comparison"]
        assert "138,000 tCO2e" in yoy
        assert "145,000 tCO2e" in yoy
        assert "-7,000 tCO2e" in yoy
        assert "reduction" in yoy

    def test_year_over_year_present_when_previous_supplied(self):
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(_computed_data(previous=6000)),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        assert "Previous Year" in sections["year_over_year_comparison"]

    def test_year_over_year_placeholder_when_missing(self):
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(_computed_data(previous=None)),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        assert "No prior-year" in sections["year_over_year_comparison"]

    def test_reduction_commitments_placeholder_when_empty(self):
        sections = from_json(
            self.node(
                {
                    "emissions_data": to_json(_computed_data(reduction_commitments=[])),
                    "caller_trust_level": TrustLevel.ANONYMOUS.value,
                }
            )["report_sections"]
        )
        assert "No reduction commitments" in sections["reduction_commitments"]

    def test_missing_emissions_data_returns_error(self):
        result = self.node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── ComplianceCheckNode (inner domain node 4, ANONYMOUS) ───────────────────────


class TestComplianceCheckNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.compliance_check_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.compliance_check_node import ComplianceCheckNode

        self.node = ComplianceCheckNode()

    def test_reporting_required_over_mandatory_threshold(self):
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(total_tco2=5000.0)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["reporting_required"] is True
        flags = from_json(result["compliance_flags"])
        assert flags["reporting_required"] is True
        assert flags["gx_ets_applicable"] is False

    def test_gx_ets_applicable_over_large_threshold(self):
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(total_tco2=150000.0)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["reporting_required"] is True
        flags = from_json(result["compliance_flags"])
        assert flags["gx_ets_applicable"] is True

    def test_not_required_under_mandatory_threshold(self):
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(total_tco2=2999.0)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["reporting_required"] is False
        flags = from_json(result["compliance_flags"])
        assert flags["reporting_required"] is False

    def test_compliance_flags_carry_thresholds(self):
        flags = from_json(
            self.node({"emissions_data": to_json(_computed_data()), "caller_trust_level": TrustLevel.ANONYMOUS.value})[
                "compliance_flags"
            ]
        )
        assert flags["threshold_mandatory_tco2"] == 3000
        assert flags["threshold_gx_ets_tco2"] == 100000
        assert "温対法" in flags["regulatory_basis"]

    def test_surfaced_total_is_grid_rounded_and_reason_carries_no_full_precision(self):
        # The determination compares the full-precision total, but the values
        # placed in the caller-facing dict sit on the 1,000 grid.
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(total_tco2=137_719.512)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        flags = from_json(result["compliance_flags"])
        assert flags["total_tco2_nearest_1000"] == 138_000
        assert flags["total_tco2_nearest_1000"] % 1000 == 0
        assert "137,719" not in flags["reason"]
        assert "138,000" in flags["reason"]

    def test_determination_uses_full_precision_not_the_rounded_figure(self):
        # 2,600 tCO2e rounds UP to 3,000 on the grid but is BELOW the 3,000
        # threshold — the determination must follow the real total, so a
        # rounding artefact can never manufacture a reporting obligation.
        result = self.node(
            {
                "emissions_data": to_json(_computed_data(total_tco2=2_600.0)),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["reporting_required"] is False
        flags = from_json(result["compliance_flags"])
        assert flags["total_tco2_nearest_1000"] == 3_000

    def test_missing_emissions_data_returns_error(self):
        result = self.node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── OutputFormatNode (inner domain node 5, ANONYMOUS) ──────────────────────────


class TestOutputFormatNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.output_format_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.output_format_node import OutputFormatNode

        self.node = OutputFormatNode()

    def _state(self, reporting_required=True):
        sections = {
            "emissions_summary": "Reporting Year: 2025\nTotal Emissions: 138,000 tCO2e",
            "calculation_methodology": "GHG Protocol + 環境省 factors.",
            "regulatory_framework": "温対法 / GX-ETS.",
            "year_over_year_comparison": "Previous Year: 145,000 tCO2e",
            "reduction_commitments": "  1. Carbon neutrality by FY2050.",
        }
        compliance = {
            "reporting_required": reporting_required,
            "gx_ets_applicable": False,
            "reason": "Total emissions (138,000 tCO2e, nearest 1,000) >= 温対法 threshold (3,000 tCO2e)",
            "regulatory_basis": "地球温暖化対策の推進に関する法律 (温対法) 算定・報告・公表制度",
        }
        return {
            "report_sections": to_json(sections),
            "compliance_flags": to_json(compliance),
            "emissions_data": to_json(_emissions_data()),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }

    def test_assembles_full_report(self):
        result = self.node(self._state())
        assert result["status"] == AgentStatus.SUCCESS.value
        report = result["compliance_report"]
        assert result["result"] == report
        assert "EMISSIONS COMPLIANCE REPORT" in report
        for header in (
            "1. Emissions Summary (Scope 1/2/3)",
            "2. Calculation Methodology",
            "3. Regulatory Framework",
            "4. Year-over-Year Comparison",
            "5. Reduction Commitments",
            "REGULATORY COMPLIANCE NOTE",
            "DISCLAIMER",
        ):
            assert header in report, f"missing section header: {header}"

    def test_reporting_required_note_yes(self):
        report = self.node(self._state(reporting_required=True))["compliance_report"]
        line = [row for row in report.splitlines() if "Reporting Required" in row][0]
        assert "YES" in line

    def test_reporting_required_note_no(self):
        report = self.node(self._state(reporting_required=False))["compliance_report"]
        line = [row for row in report.splitlines() if "Reporting Required" in row][0]
        assert "NO" in line

    def test_missing_sections_returns_error(self):
        result = self.node(
            {"emissions_data": to_json(_emissions_data()), "caller_trust_level": TrustLevel.ANONYMOUS.value}
        )
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── PostProcessNode (outer post_process, output gate, ANONYMOUS) ───────────────


class TestPostProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.post_process_node import PostProcessNode

        self.node = PostProcessNode()

    def test_clean_report_passes_gate(self):
        report = "EMISSIONS COMPLIANCE REPORT\nFacility ID: ENE-FAC-20260712-001\nAll clear."
        result = self.node(
            {
                "compliance_report": report,
                "reporting_required": True,
                "enriched_context": to_json({"facility_id": "ENE-FAC-20260712-001"}),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        # The identifier's embedded digit runs are structure, not figures —
        # they survive the grid scan byte-identical.
        assert result["formatted_output"] == report
        assert result["result"] == report

    def test_empty_report_uses_fallback(self):
        result = self.node(
            {"compliance_report": "", "reporting_required": False, "caller_trust_level": TrustLevel.ANONYMOUS.value}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "No report content generated" in result["formatted_output"]

    def test_gate_redacts_credential_leak(self):
        """A blocked response must also be CONTAINED, not merely flagged.

        This used to assert ``formatted_output == result`` — i.e. that the
        refusal notice was mirrored into ``result``. That pinned only half the
        contract: ``result`` is the PRE-gate value and the envelope resolves
        ``formatted_output or result`` without consulting status, so the
        clearing of every caller-facing field is what actually contains the
        refusal. Strengthened to assert that clearing.
        """
        from src.nodes.post_process_node import _CLEARED_ON_VIOLATION

        leaky = "EMISSIONS COMPLIANCE REPORT\ntoken=sk-abcdefghij0123456789ABCDEF"
        result = self.node(
            {"compliance_report": leaky, "reporting_required": True, "caller_trust_level": TrustLevel.ANONYMOUS.value}
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "REDACTED" in result["formatted_output"]
        # Truthy on purpose: a falsy replacement ACTIVATES the envelope's
        # `formatted_output or result` fallback instead of suppressing it.
        assert result["formatted_output"]
        assert (result["formatted_output"] or result["result"]) is result["formatted_output"]
        # Every caller-facing field is overwritten, not just the status.
        for field, expected in _CLEARED_ON_VIOLATION.items():
            assert result[field] == expected, f"{field} not contained on the refusal path"
        assert result["result"] is None
        assert "sk-abcdefghij0123456789ABCDEF" not in json.dumps(result)
        assert "EMISSIONS COMPLIANCE REPORT" not in result["error_log"][0]
        assert any("credential pattern" in e for e in result["error_log"])

    def test_security_gate_output_helper_detects_and_clears(self):
        from src.nodes.post_process_node import _security_gate_output

        assert _security_gate_output("sk-abcdefghij0123456789ABCDEF") is not None
        assert _security_gate_output("Bearer abcdefgh12345678") is not None
        assert _security_gate_output("password = supersecret123") is not None
        assert _security_gate_output("A perfectly clean emissions report.") is None

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── Graph wiring: outer AgentBaseGraph + inner BaseGraph (Cat 2 nested) ─────────


class TestOuterGraphComposition:
    def test_registers_five_backbone_slots(self):
        from src.graph.graph import (
            EmissionsComplianceReportGeneratorAgent,
            EmissionsReportGraphNode,
        )
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        agent = EmissionsComplianceReportGeneratorAgent()
        agent.compile()
        assert set(agent._nodes.keys()) == {
            "initialize",
            "pre_process",
            "main",
            "post_process",
            "finalize",
        }
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], EmissionsReportGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.graph import EmissionsComplianceReportGeneratorAgent

        agent = EmissionsComplianceReportGeneratorAgent()
        assert agent.name == "EmissionsComplianceReportGeneratorAgent"
        assert agent.state_schema is State

    def test_graph_alias_matches_real_class(self):
        from src.graph.graph import Graph, EmissionsComplianceReportGeneratorAgent

        assert Graph is EmissionsComplianceReportGeneratorAgent

    def test_main_slot_graphnode_contracts(self):
        from src.graph.graph import EmissionsReportGraphNode

        node = EmissionsReportGraphNode()
        assert node.error_strategy == "propagate"
        assert node.propagate_hitl is False
        # extract_input prefers validated_input, falls back to user_input
        assert node.extract_input({"validated_input": "V", "user_input": "U"}) == "V"
        assert node.extract_input({"user_input": "U"}) == "U"

    def test_merge_output_maps_subresult_keys(self):
        from src.graph.graph import EmissionsReportGraphNode

        node = EmissionsReportGraphNode()
        sub_result = {
            "compliance_report": "REPORT",
            "report_sections": "{}",
            "compliance_flags": "{}",
            "reporting_required": True,
            "status": AgentStatus.SUCCESS.value,
            "node_history": ["x"],  # not forwarded by merge_output
        }
        delta = node.merge_output({}, sub_result)
        assert delta["compliance_report"] == "REPORT"
        assert delta["reporting_required"] is True
        assert delta["status"] == AgentStatus.SUCCESS.value
        # nothing declined this run, so the reason slot is carried through empty
        assert delta["error_code"] == ""
        assert set(delta.keys()) == {
            "error_code",
            "compliance_report",
            "report_sections",
            "compliance_flags",
            "reporting_required",
            "status",
        }


class TestInnerDomainGraph:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "input_validate_node",
            "parse_emissions_data_node",
            "generate_report_sections_node",
            "compliance_check_node",
            "output_format_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def test_registers_five_domain_nodes(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.register_nodes()
        assert set(g._nodes.keys()) == {
            "input_validate",
            "parse_emissions_data",
            "generate_report_sections",
            "compliance_check",
            "output_format",
        }

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        assert g.name == "ene_c2_007_emissions_compliance_report_workflow"
        assert g.state_schema is State

    def test_inner_graph_invoke_produces_report(self):
        """Standalone inner-graph invoke (ANONYMOUS caller) runs the linear pipeline
        and shapes the get_output() dict consumed by the outer merge_output()."""
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = g.invoke(VALID_PAYLOAD, ctx=ctx)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["compliance_report"] is not None
        assert result["reporting_required"] is True
        assert "EMISSIONS COMPLIANCE REPORT" in result["compliance_report"]


# ── InputValidateNode: caller numerics are finite, bounded, fail-closed ────────
# Non-finite / negative / malformed / over-magnitude measurements must be
# rejected with a controlled ERROR BEFORE the reporting logic — a NaN parses
# via float() (and arrives via raw JSON), and NaN comparisons are always
# False, so an unchecked value silently understates emissions or flips the
# mandatory-reporting decision. Errors name the field, never the value.

_NON_FINITE_VALUES = [
    float("nan"),
    float("inf"),
    float("-inf"),
    "NaN",
    "Infinity",
    "-Infinity",
]


class TestInputValidateNumericGuards:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.input_validate_node import InputValidateNode

        self.node = InputValidateNode()

    def _run_payload(self, payload):
        return self.node(
            {
                "validated_input": json.dumps(payload),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )

    def _run_scope1_qty(self, qty):
        return self._run_payload(
            _emissions_payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": qty}],
                scope2_activities=[],
                scope3_activities=[],
            )
        )

    def _run_scope2(self, **fields):
        act = {"energy_type": "on_site_biomass", "quantity": 1000}
        act.update(fields)
        return self._run_payload(
            _emissions_payload(
                scope1_activities=[],
                scope3_activities=[],
                scope2_activities=[act],
            )
        )

    def _run_scope3_emissions(self, value):
        return self._run_payload(
            _emissions_payload(
                scope1_activities=[],
                scope2_activities=[],
                scope3_activities=[{"category": "x", "emissions_tco2": value}],
            )
        )

    @staticmethod
    def _assert_rejected_naming_field(result, field_fragment):
        assert result["status"] == AgentStatus.SUCCESS.value
        joined = " ".join(result["error_log"])
        assert field_fragment in joined, f"error must name the field: {joined}"

    # — parametrized non-finite matrix, per caller-controlled numeric field —

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES + [-5000, "not-a-number", True, 1e13])
    def test_scope1_quantity_rejects_bad_values(self, bad):
        self._assert_rejected_naming_field(self._run_scope1_qty(bad), "scope1_activities[0].quantity")

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES + [-1, True, 1e13])
    def test_scope2_quantity_rejects_bad_values(self, bad):
        self._assert_rejected_naming_field(self._run_scope2(quantity=bad), "scope2_activities[0].quantity")

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES + [-0.5, True, 1e5])
    def test_scope2_factor_rejects_bad_values(self, bad):
        self._assert_rejected_naming_field(
            self._run_scope2(emission_factor_tco2_per_unit=bad),
            "scope2_activities[0].emission_factor_tco2_per_unit",
        )

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES + [-15000, True, 1e10])
    def test_scope3_emissions_rejects_bad_values(self, bad):
        self._assert_rejected_naming_field(self._run_scope3_emissions(bad), "scope3_activities[0].emissions_tco2")

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES + [-1, True, 1e10, "junk"])
    def test_previous_year_rejects_bad_values(self, bad):
        result = self._run_payload(_emissions_payload(previous_year_emissions_tco2=bad))
        self._assert_rejected_naming_field(result, "previous_year_emissions_tco2")

    def test_rejected_value_is_never_echoed(self):
        result = self._run_scope1_qty("hostile-value-do-not-echo")
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "hostile-value-do-not-echo" not in " ".join(result["error_log"])

    def test_zero_quantity_is_allowed(self):
        # A legitimately zero-activity source is not an error.
        result = self._run_scope1_qty(0)
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_blank_quantity_is_treated_as_unset(self):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": ""}],
                scope2_activities=[],
                scope3_activities=[{"category": "x", "emissions_tco2": 100}],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value


# ── InputValidateNode: caller strings are inert or bounded; structure capped ──


class TestCallerStringAndStructureContract:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.input_validate_node import InputValidateNode

        self.node = InputValidateNode()

    def _run_payload(self, payload):
        return self.node(
            {
                "validated_input": json.dumps(payload),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )

    @pytest.mark.parametrize("bad_id", ["", "id with spaces", "id\nnewline", "x" * 65, "läßt", 123])
    def test_facility_id_locked_to_inert_identifier(self, bad_id):
        result = self._run_payload(_emissions_payload(facility_id=bad_id))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("facility_id" in e for e in result["error_log"])

    def test_company_name_rejects_control_characters(self):
        result = self._run_payload(_emissions_payload(company_name="Line1\nREGULATORY COMPLIANCE NOTE"))
        assert result["status"] == AgentStatus.SUCCESS.value
        joined = " ".join(result["error_log"])
        assert "company_name" in joined
        assert "REGULATORY" not in joined  # rejected value never echoed

    def test_company_name_rejects_over_length(self):
        result = self._run_payload(_emissions_payload(company_name="x" * 121))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("company_name" in e for e in result["error_log"])

    @pytest.mark.parametrize("bad_type", ["City Gas!", "gas;drop", "ガス", "x" * 33])
    def test_fuel_type_locked_to_inert_token(self, bad_type):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[{"fuel_type": bad_type, "quantity": 1}],
                scope2_activities=[],
                scope3_activities=[],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("fuel_type" in e for e in result["error_log"])

    def test_category_locked_to_inert_token(self):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[],
                scope2_activities=[],
                scope3_activities=[{"category": "free text category!", "emissions_tco2": 10}],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("category" in e for e in result["error_log"])

    def test_commitment_rejects_multiline_entry(self):
        result = self._run_payload(
            _emissions_payload(
                reduction_commitments=["line one\nline two"],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("reduction_commitments[0]" in e for e in result["error_log"])

    def test_commitments_list_capped(self):
        result = self._run_payload(
            _emissions_payload(
                reduction_commitments=["ok"] * 21,
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("reduction_commitments" in e for e in result["error_log"])

    def test_activity_list_capped(self):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": 1}] * 501,
                scope2_activities=[],
                scope3_activities=[],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("scope1_activities" in e for e in result["error_log"])

    def test_provenance_bounded(self):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[],
                scope3_activities=[],
                scope2_activities=[
                    {
                        "energy_type": "on_site_biomass",
                        "quantity": 1,
                        "emission_factor_tco2_per_unit": 0.1,
                        "emission_factor_provenance": "p" * 201,
                    }
                ],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("emission_factor_provenance" in e for e in result["error_log"])

    def test_undocumented_activity_keys_are_dropped(self):
        result = self._run_payload(
            _emissions_payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": 1, "smuggled": "content"}],
                scope2_activities=[],
                scope3_activities=[],
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["emissions_data"])
        assert "smuggled" not in data["scope1_activities"][0]


# ── ParseEmissionsDataNode: Scope 2 factor provenance (MEDIUM finding, #12) ────
# Scope 2 factors come from a version-pinned authoritative dataset; a caller
# factor can never override an authoritative one, and any non-authoritative
# factor forces a non-final (review-required) determination downstream.


class TestParseScope2FactorProvenance:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.parse_emissions_data_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.parse_emissions_data_node import ParseEmissionsDataNode

        self.node = ParseEmissionsDataNode()

    def _run(self, scope2):
        state = {
            "emissions_data": to_json(_emissions_data(scope1=[], scope2=scope2, scope3=[])),
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        return from_json(self.node(state)["emissions_data"])

    def test_authoritative_factor_used_for_known_energy(self):
        data = self._run([{"energy_type": "grid_electricity", "quantity": 1000000}])
        assert data["scope2_tco2"] == 434.0
        assert data["factor_review_required"] is False
        assert data["scope2_factor_dataset_version"]
        assert data["scope2_factor_provenance"]

    def test_caller_factor_cannot_lower_known_authoritative_factor(self):
        # A fabricated lowball caller factor for grid electricity is IGNORED —
        # the authoritative factor is used, so the reported figure is NOT lowered.
        data = self._run(
            [
                {
                    "energy_type": "grid_electricity",
                    "quantity": 1000000,
                    "emission_factor_tco2_per_unit": 0.0000001,  # fabricated low value
                }
            ]
        )
        assert data["scope2_tco2"] == 434.0  # authoritative 0.000434, not the low factor
        assert data["factor_review_required"] is False
        assert any("scope2_caller_factor_ignored_authoritative_used" in f for f in data["data_quality_flags"])

    def test_unknown_energy_without_provenance_is_unapproved_and_review_required(self):
        data = self._run(
            [
                {
                    "energy_type": "on_site_biomass",
                    "quantity": 1000,
                    "emission_factor_tco2_per_unit": 0.2,
                }
            ]
        )
        assert data["factor_review_required"] is True
        assert any("unapproved_scope2_factor" in f for f in data["data_quality_flags"])
        rec = data["supplied_factor_records"][0]
        assert rec["provenance"] is None
        assert rec["status"] == "unapproved_pending_review"

    def test_unknown_energy_with_provenance_applies_and_records(self):
        data = self._run(
            [
                {
                    "energy_type": "on_site_biomass",
                    "quantity": 1000,
                    "emission_factor_tco2_per_unit": 0.2,
                    "emission_factor_provenance": "Supplier ISO-14064 verified factor FY2024",
                }
            ]
        )
        assert data["scope2_tco2"] == 200.0  # 1000 × 0.2
        assert data["factor_review_required"] is True
        rec = data["supplied_factor_records"][0]
        assert rec["provenance"] == "Supplier ISO-14064 verified factor FY2024"
        assert rec["status"] == "supplied_pending_review"


# ── ComplianceCheckNode: threshold boundaries + non-final review (finding #12) ─


class TestComplianceBoundaryAndReview:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.compliance_check_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.compliance_check_node import ComplianceCheckNode

        self.node = ComplianceCheckNode()

    def _run(self, total, **extra):
        data = _computed_data(total_tco2=total)
        data.update(extra)
        return self.node({"emissions_data": to_json(data), "caller_trust_level": TrustLevel.ANONYMOUS.value})

    def test_exactly_mandatory_threshold_is_required(self):
        assert self._run(3000.0)["reporting_required"] is True

    def test_just_below_mandatory_threshold_not_required(self):
        assert self._run(2999.999)["reporting_required"] is False

    def test_exactly_gx_ets_threshold_applicable(self):
        flags = from_json(self._run(100000.0)["compliance_flags"])
        assert flags["gx_ets_applicable"] is True

    def test_just_below_gx_ets_threshold_not_applicable(self):
        flags = from_json(self._run(99999.999)["compliance_flags"])
        assert flags["gx_ets_applicable"] is False

    def test_clean_result_is_final(self):
        flags = from_json(self._run(5000.0)["compliance_flags"])
        assert flags["review_required"] is False
        assert flags["final_ready"] is True

    def test_factor_review_forces_non_final(self):
        flags = from_json(self._run(5000.0, factor_review_required=True)["compliance_flags"])
        assert flags["review_required"] is True
        assert flags["final_ready"] is False

    def test_unapproved_factor_cannot_produce_final_not_required(self):
        # A sub-threshold total obtained WITH a non-authoritative factor must NOT
        # be presentable as a final "no reporting required" determination.
        flags = from_json(self._run(1500.0, factor_review_required=True)["compliance_flags"])
        assert flags["reporting_required"] is False
        assert flags["final_ready"] is False  # cannot silently pass as final


# ── Trust gate (PreProcessNode — the only VERIFIED_EXTERNAL node) ─────────────


class TestTrustGate:
    """Trust-gate coverage for the outer pre_process slot.

    PreProcessNode is the only node in this template requiring VERIFIED_EXTERNAL
    trust. These tests invoke it through __call__ (the real BaseNode entry
    point) so the trust gate actually runs — a direct execute() call bypasses
    it. On denial the framework RETURNS an error dict; it never raises.
    """

    # PII-free positive payload (no personal names / emails / digit groups),
    # so the framework input gate does not mask any asserted field.
    _CLEAN_PAYLOAD = json.dumps(
        {
            "facility_id": "ENE-FAC-0001",
            "reporting_year": 2025,
            "scope1_activities": [{"fuel_type": "coal", "quantity": 1000}],
        }
    )

    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.pre_process_node import PreProcessNode

        self.node = PreProcessNode()

    def test_gate_rejects_untrusted_caller_before_execute(self):
        # ANONYMOUS < required VERIFIED_EXTERNAL: the gate in __call__ denies
        # BEFORE execute() runs and RETURNS an error dict (no exception raised).
        result = self.node(
            {
                "user_input": self._CLEAN_PAYLOAD,
                "input_context": {},
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "trust gate denied" in " ".join(result["error_log"])
        # execute() never ran, so its output key is absent.
        assert "validated_input" not in result

    def test_gate_admits_trusted_caller(self):
        # VERIFIED_EXTERNAL caller clears the gate; execute() runs and succeeds.
        result = self.node(
            {
                "user_input": self._CLEAN_PAYLOAD,
                "input_context": {},
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None
