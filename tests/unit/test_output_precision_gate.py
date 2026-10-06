# ENE-C2-007 — Output-boundary contract: the precision grid and its layers
#
# The published report schema states that every emissions figure is an
# aggregate rounded to the nearest 1,000 tCO2e. The renderers produce figures
# on that grid; PostProcessNode's gate independently ENFORCES it, so a
# rendering regression cannot leak a full-precision figure.
#
# These tests probe the gate from BOTH directions:
#   - every representation of an off-grid figure is snapped (grouped, bare
#     5+-digit runs, short values in unit-marker context on either side,
#     attached or separated by any whitespace run, signed or unsigned);
#   - structural tokens (horizons, counts, years, embedded acronyms, on-grid
#     figures) come through byte-identical.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.post_process_node import (
    _enforce_precision,
    _redact_blocked_fields,
    _security_gate_output,
)
from src.schemas.state import from_json, to_json


# ── The grid is enforced for every off-grid representation ────────────────────


class TestPrecisionGridSnapsEveryForm:
    @pytest.mark.parametrize(
        "text,expected",
        [
            # comma-grouped, any magnitude — no magnitude exemption
            ("Total: 9,999 tCO2e", "Total: 10,000 tCO2e"),
            ("Total: 1,234,567 tCO2e", "Total: 1,235,000 tCO2e"),
            # bare 5+-digit runs (a rendering regression)
            ("Total: 137719 tCO2e", "Total: 138,000 tCO2e"),
            ("Total: 99999 tCO2e", "Total: 100,000 tCO2e"),
            # short value in marker context — marker BEFORE the value
            ("JPY 9999", "JPY 10,000"),
            ("EUR 1234", "EUR 1,000"),
            # marker AFTER the value (symmetric)
            ("9999 JPY", "10,000 JPY"),
            ("1234 USD", "1,000 USD"),
            # attached symbols, either side
            ("¥9999", "¥10,000"),
            ("￥9999", "￥10,000"),
            ("$1234", "$1,000"),
            ("9999円", "10,000円"),
            # signed values — the sign is preserved
            ("JPY-9999", "JPY-10,000"),
            ("JPY +9999", "JPY +10,000"),
            ("-9999 JPY", "-10,000 JPY"),
            ("Change: +1,234 tCO2e", "Change: +1,000 tCO2e"),
            # arbitrary whitespace delimiters (group grammar, not lookbehinds)
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY\n9999", "JPY\n10,000"),
            ("JPY \t \n 9999", "JPY \t \n 10,000"),
        ],
    )
    def test_off_grid_forms_are_snapped(self, text, expected):
        result, redactions = _enforce_precision(text)
        assert result == expected
        assert redactions == 1

    @pytest.mark.parametrize(
        "text",
        [
            # already on the grid — byte-identical
            "Total: 138,000 tCO2e",
            "Total: 1,000 tCO2e",
            "JPY 1,000",
            "JPY 9000",
            "-3,000 tCO2e",
            # structural tokens: horizons, versions, counts, years, acronyms
            "retention horizon 90d",
            "schema v12",
            "5 activity records processed",
            "Reporting Year: 2025",
            "in 2026 the scheme starts",
            "STAR 2026 conference",
            "dataset MOE-METI-SANHO-2024.1",
            "GX推進法 (2023) GX-ETS",
        ],
    )
    def test_on_grid_and_structural_tokens_are_untouched(self, text):
        result, redactions = _enforce_precision(text)
        assert result == text, f"token must be byte-identical: {text!r} -> {result!r}"
        assert redactions == 0

    def test_grouped_value_after_marker_snaps_as_a_whole_token(self):
        # Leftmost-first matching must not consume "JPY 1" out of "JPY 1,234"
        # (which would corrupt it into "JPY 0,234"): the comma-grouped form
        # comes FIRST in the marker-then-value alternation.
        result, redactions = _enforce_precision("JPY 1,234")
        assert result == "JPY 1,000"
        assert redactions == 1

    def test_on_grid_grouped_value_after_marker_is_byte_identical(self):
        result, redactions = _enforce_precision("JPY 1,000")
        assert result == "JPY 1,000"
        assert redactions == 0

    def test_every_token_in_a_multi_figure_line_is_snapped(self):
        result, redactions = _enforce_precision("Scope 1: 12,345 tCO2e; Scope 2: 6789 JPY; Scope 3: 98765 tCO2e")
        assert "12,000" in result and "12,345" not in result
        assert "7,000 JPY" in result and "6789" not in result
        assert "99,000" in result and "98765" not in result
        assert redactions == 3


# ── A decimal is part of its figure, never a figure of its own ────────────────


class TestPrecisionGridReadsDecimalsAsOneFigure:
    """A fraction belongs to the number in front of it.

    The grammar this gate inherited treated the fraction of "9999.99999" as a
    standalone five-digit run and rewrote it to "9999.100,000"; an off-grid
    "JPY 1234.56" had its integer part snapped while the fraction dangled,
    yielding "JPY 1,000.56" — neither the true figure nor a figure on the grid.
    Energy reporting renders tariff rates, kWh readings and percentages, so
    both halves matter here: an off-grid figure must still snap (as ONE
    number), and a rate that is not a figure must survive untouched.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            # The decimal part belongs to the amount and snaps WITH it — one
            # token, one snap, no dangling fraction.
            ("JPY 1234.56", "JPY 1,000"),
            ("JPY 1,234,567.89", "JPY 1,235,000"),
            ("9999.99999 JPY", "10,000 JPY"),
            # Form-based: a 5+-digit run carrying a fraction is still a figure.
            ("Total: 137719.512 tCO2e", "Total: 138,000 tCO2e"),
            ("Total: 1,234,567.89 tCO2e", "Total: 1,235,000 tCO2e"),
            ("Meter reading 12345.6789 kWh", "Meter reading 12,000 kWh"),
            # The exact string section 4 renders — see the node-level test below.
            (
                "Change:         +134,000 tCO2e (+13400.0%) — increase",
                "Change:         +134,000 tCO2e (+13,000%) — increase",
            ),
            # A figure that CLOSES A SENTENCE must still snap: the trailing
            # sentence period is punctuation, not a decimal point. This is why
            # the decimal joins the LEADING guard only — a trailing guard that
            # knew about "." would let this amount escape the grid entirely.
            ("The book totals JPY 9999.", "The book totals JPY 10,000."),
            ("Total: 137719. Next section.", "Total: 138,000. Next section."),
        ],
    )
    def test_off_grid_decimal_figure_snaps_as_one_number(self, text, expected):
        result, redactions = _enforce_precision(text)
        assert result == expected
        assert redactions == 1

    @pytest.mark.parametrize(
        "text",
        [
            # An on-grid amount stays byte-identical even carrying a fraction.
            "JPY 1,000.00",
            # Emission factors and intensities: rates, not aggregates. Their
            # fractions run long and are NOT five-digit figures.
            "Emission factor: 0.512345 tCO2e/MWh",
            "Grid intensity: 0.000434 tCO2e/kWh",
            "Load factor 0.87654321",
            # Percentages — the headline corruption case, plus the run length
            # that needs the DIGIT half of the leading guard: with only the
            # decimal point guarded, the scan would restart one digit into
            # "999999" and snap the five digits it found there.
            "Total Return | 9999.99999%",
            "Availability 99.999999%",
            "Renewable share: 42.98765%",
            "Year-on-year change: -3.25%",
            "Reduction target: 46.0% by 2030",
            # Version tags and dotted structure carry digit runs that are not
            # figures at any magnitude.
            "schema v1.2345",
            "pinned to v12.34567 of the factor set",
            "10.20.30.40",
            "Reporting period 2024-04-01 to 2025-03-31",
        ],
    )
    def test_rates_versions_and_dates_survive_byte_identical(self, text):
        result, redactions = _enforce_precision(text)
        assert result == text, f"token must be byte-identical: {text!r} -> {result!r}"
        assert redactions == 0

    def test_suffixed_decimal_cannot_backtrack_into_a_dangling_fraction(self):
        """The absorption must hold even when a suffix rejects the match.

        A bare-optional fraction — `(?:\.\d+)?` — is a backtracking point:
        the "m" behind ".56" fails the trailing guard, the engine gives the
        fraction back and matches "1234" alone, and the split this class
        pins returns as "JPY 1,000.56m". The two-arm absorption takes the
        fraction whole or asserts there is none, so the suffixed token is
        byte-identical while the plain amount still snaps as ONE number.
        """
        assert _enforce_precision("JPY 1234.56m") == ("JPY 1234.56m", 0)
        assert _enforce_precision("JPY 1234.56") == ("JPY 1,000", 1)


# ── The three layers stay independent, each with its own outcome ──────────────


class TestOutputGateLayers:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.post_process_node import PostProcessNode

        self.node = PostProcessNode()

    def test_credential_scan_covers_the_structured_representations(self):
        # A credential that never reaches the report TEXT but sits in the
        # structured section payload must still withhold the response.
        result = self.node(
            {
                "compliance_report": "EMISSIONS COMPLIANCE REPORT\nclean text",
                "report_sections": json.dumps({"emissions_summary": "token=sk-abcdefghij0123456789ABCDEF"}),
                "compliance_flags": json.dumps({"reporting_required": True}),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "REDACTED" in result["formatted_output"]
        assert "sk-abcdefghij0123456789ABCDEF" not in result["formatted_output"]

    def test_verbatim_caller_payload_is_redacted(self):
        payload = json.dumps({"facility_id": "ENE-FAC-0001", "reporting_year": 2025})
        result = self.node(
            {
                "compliance_report": f"EMISSIONS COMPLIANCE REPORT\nEcho: {payload}",
                "validated_input": payload,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert payload not in result["formatted_output"]
        assert "[REDACTED]" in result["formatted_output"]

    def test_precision_grid_applies_to_report_sections_and_flags(self):
        result = self.node(
            {
                "compliance_report": "Total Emissions: 137,719 tCO2e",
                "report_sections": to_json({"emissions_summary": "Total: 137,719 tCO2e"}),
                "compliance_flags": to_json(
                    {
                        "reporting_required": True,
                        "reason": "Total emissions (137,719 tCO2e) >= threshold",
                        "total_tco2_nearest_1000": 137_719,
                    }
                ),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "138,000" in result["formatted_output"]
        assert "137,719" not in result["formatted_output"]

        sections = from_json(result["report_sections"])
        assert "138,000" in sections["emissions_summary"]
        assert "137,719" not in sections["emissions_summary"]

        flags = from_json(result["compliance_flags"])
        assert flags["total_tco2_nearest_1000"] == 138_000
        assert "137,719" not in flags["reason"]

    def test_rendered_percentage_snaps_as_one_number(self):
        # This is the decimal that actually REACHES shipped output. Section 4
        # renders "{delta:+,d} tCO2e ({pct:+.1f}%)", and a facility whose
        # previous-year total was small pushes that percentage past five
        # digits — so the gate reads it as a figure BY FORM and snaps it, as
        # it does any 5+-digit run. Before the fraction joined the token the
        # integer part snapped while the ".0" was left behind, and the report
        # shipped "(+13,000.0%)": neither the computed percentage nor a value
        # on the grid. Driving /invoke with previous_year_emissions_tco2=1000
        # reproduced exactly that.
        result = self.node(
            {
                "compliance_report": "Change:         +134,000 tCO2e (+13400.0%) — increase",
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "(+13,000%)" in result["formatted_output"]
        assert "13,000.0" not in result["formatted_output"], "the fraction must not dangle"

    def test_clean_on_grid_report_passes_through_byte_identical(self):
        report = (
            "EMISSIONS COMPLIANCE REPORT (DRAFT)\n"
            "Reporting Year: 2025\n"
            "Total Emissions: 138,000 tCO2e\n"
            "温対法 threshold (3,000 tCO2e); GX-ETS threshold (100,000 tCO2e)\n"
        )
        result = self.node(
            {
                "compliance_report": report,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == report

    def test_helper_detects_and_clears(self):
        assert _security_gate_output("sk-abcdefghij0123456789ABCDEF") is not None
        assert _security_gate_output("Bearer abcdefgh12345678") is not None
        assert _security_gate_output("password = supersecret123") is not None
        assert _security_gate_output("A perfectly clean emissions report.") is None

    def test_redact_helper_ignores_short_incidental_values(self):
        sanitised, redacted = _redact_blocked_fields("report body", {"user_input": "short"})
        assert sanitised == "report body"
        assert redacted == []


# ── The renderer refuses to emit a non-finite figure (defense in depth) ───────


class TestGridRendererFailsClosed:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_aggregate_is_refused(self, bad):
        from src.nodes.precision_grid import format_grid, to_grid

        with pytest.raises(ValueError, match="not finite"):
            to_grid(bad)
        with pytest.raises(ValueError, match="not finite"):
            format_grid(bad)

    @pytest.mark.parametrize(
        "value,expected",
        [(0, "0"), (499, "0"), (500, "0"), (501, "1,000"), (137_719.512, "138,000"), (-7_400, "-7,000")],
    )
    def test_aggregates_render_on_the_grid(self, value, expected):
        from src.nodes.precision_grid import format_grid

        assert format_grid(value) == expected
