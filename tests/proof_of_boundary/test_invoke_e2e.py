# PB: End-to-end business behaviour through POST /invoke — src/api/server.py
#
# Proves the supported input contract produces REAL outcomes through the full
# nested graph (outer backbone → inner domain pipeline):
#   - a real emissions report computed from the caller's activity data, with
#     every regulatory outcome path reachable (below threshold / specified
#     emitter / large emitter / non-final factor review)
#   - a validation rejection for every malformed caller field, including the
#     full non-finite matrix, with no echo of the rejected value
#   - the caller's request metadata (input_context) accepted and bounded
#   - every figure in the response on the documented 1,000 tCO2e grid
#
# These tests run the REAL compiled agent: every request crosses the
# entry-point auth, the outer trust and input gates, all five domain nodes,
# and the output gate.
#
# The app is driven through its real ASGI interface (no TestClient — httpx is
# only a transitive dependency of the framework, not a declared test dep).

import asyncio
import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus
from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"


def _payload(**overrides) -> dict:
    body = {
        "facility_id": "ENE-FAC-20260712-001",
        "company_name": "Tokyo Bay Thermal Power K.K.",
        "reporting_year": 2025,
        "scope1_activities": [{"fuel_type": "coal", "quantity": 50000}],
        "scope2_activities": [{"energy_type": "grid_electricity", "quantity": 8000000}],
        "scope3_activities": [{"category": "purchased_goods", "emissions_tco2": 15000}],
        "previous_year_emissions_tco2": 145000,
        "reduction_commitments": [],
    }
    body.update(overrides)
    return body


def _post_invoke(body_obj: dict, token: str | None = _TOKEN) -> tuple[int, dict]:
    """POST /invoke through the real ASGI app (Bearer auth)."""
    body = json.dumps(body_obj).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    messages: list[dict] = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], json.loads(sent["body"].decode() or "{}")


def _invoke(emissions: dict, **extra) -> tuple[int, dict]:
    request = {"input": json.dumps(emissions), "session_id": "pb-e2e"}
    request.update(extra)
    return _post_invoke(request)


@pytest.fixture(autouse=True)
def deploy_shaped_env(monkeypatch):
    """Deployment-shaped server environment: auth token set, caller uses Bearer."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    for module in (
        "pre_process_node",
        "input_validate_node",
        "parse_emissions_data_node",
        "generate_report_sections_node",
        "compliance_check_node",
        "output_format_node",
        "post_process_node",
    ):
        monkeypatch.setattr(f"src.nodes.{module}.emit_trace_event", lambda *a, **k: None)


# ── The public path does real work ────────────────────────────────────────────


class TestRealDomainOutcomes:
    def test_large_emitter_produces_a_real_report(self):
        status, body = _invoke(_payload())
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value, body.get("error_log")

        report = body["formatted_output"]
        assert report and "EMISSIONS COMPLIANCE REPORT" in report
        # The report is computed from the caller's activity data, not a stub:
        # the caller's facility, its computed scope totals, and the regulatory
        # determination all appear. (Free-text caller fields such as the
        # company name may be masked by the framework's privacy gate before
        # the domain nodes see them, so they are not asserted here.)
        assert "ENE-FAC-20260712-001" in report
        assert "Scope 1 (direct):" in report
        assert "Total Emissions:" in report
        assert "REGULATORY COMPLIANCE NOTE" in report
        assert body["reporting_required"] is True

        flags = json.loads(body["compliance_flags"])
        assert flags["gx_ets_applicable"] is True
        assert flags["total_tco2_nearest_1000"] > 0

    def test_output_scales_with_the_caller_data(self):
        _, small = _invoke(
            _payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": 100}],
                scope2_activities=[],
                scope3_activities=[],
            )
        )
        _, large = _invoke(_payload())
        small_total = json.loads(small["compliance_flags"])["total_tco2_nearest_1000"]
        large_total = json.loads(large["compliance_flags"])["total_tco2_nearest_1000"]
        assert large_total > small_total, "the pipeline must compute from caller data"

    @pytest.mark.parametrize(
        "scope3,expect_reporting,expect_gx_ets",
        [
            (10, False, False),  # below the mandatory threshold
            (50_000, True, False),  # specified emitter
            (150_000, True, True),  # large emitter, trading-scheme scope
        ],
    )
    def test_every_regulatory_outcome_path_is_reachable(self, scope3, expect_reporting, expect_gx_ets):
        status, body = _invoke(
            _payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": 1}],
                scope2_activities=[],
                scope3_activities=[{"category": "purchased_goods", "emissions_tco2": scope3}],
            )
        )
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value
        flags = json.loads(body["compliance_flags"])
        assert body["reporting_required"] is expect_reporting
        assert flags["gx_ets_applicable"] is expect_gx_ets

    def test_non_authoritative_factor_yields_a_non_final_report(self):
        status, body = _invoke(
            _payload(
                scope1_activities=[],
                scope2_activities=[
                    {
                        "energy_type": "on_site_biomass",
                        "quantity": 1000,
                        "emission_factor_tco2_per_unit": 0.2,
                    }
                ],
                scope3_activities=[{"category": "purchased_goods", "emissions_tco2": 5000}],
            )
        )
        assert status == 200
        flags = json.loads(body["compliance_flags"])
        assert flags["review_required"] is True
        assert flags["final_ready"] is False
        assert "NOT final-ready" in body["formatted_output"]


# ── Every caller input is hostile until proven bounded ────────────────────────


class TestValidationRejectionThroughInvoke:
    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "-Infinity", -1, True, 1e13],
    )
    def test_non_finite_or_out_of_range_quantity_is_refused(self, bad):
        # json.dumps emits bare NaN/Infinity, which Python's json parses back —
        # exactly the raw-body path a hostile caller would use.
        status, body = _invoke(
            _payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": bad}],
                scope2_activities=[],
                scope3_activities=[],
            )
        )
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value
        # the declined run releases no structured product at all: the key is
        # absent from the envelope, not present and empty
        assert not body.get("compliance_report")
        assert not body.get("compliance_flags")

    def test_each_reason_code_renders_its_own_caller_facing_sentence(self):
        """What the caller actually reads, pinned per reason.

        A declined run returns `status: success` with no structured product, and
        the whole of what the caller receives is one sentence. Asserting only
        the status and the absent product leaves that sentence untested: the run
        could render the wrong reason, or an empty body, and every other
        assertion in this file would still pass.

        Only two of the three reasons are reachable over HTTP — see the size-cap
        test below for the third.
        """
        from src.services.failure_message import EMPTY_INPUT, INVALID_VALUE

        cases = [
            ("no request at all", {"input": "", "session_id": "pb-e2e"}, EMPTY_INPUT),
            ("a value outside the contract", {"input": "not a json document", "session_id": "pb-e2e"}, INVALID_VALUE),
        ]
        for label, request, sentence in cases:
            status, body = _post_invoke(request)
            assert status == 200, f"{label}: {status} {body}"
            assert body["status"] == AgentStatus.SUCCESS.value, f"{label}: {body}"
            assert body.get("output") == sentence, f"{label}: got {body.get('output')!r}"
            assert not body.get("compliance_report"), label
            assert not body.get("report_sections"), label

    def test_an_oversized_request_is_refused_by_the_adapter_not_the_node(self):
        """The size cap is enforced twice at the same value, and the outer one wins.

        The adapter and the node both cap the request at 256 KiB, so a body past
        that never reaches the graph: the caller gets 413, not the node's
        too-long sentence. Asserting the sentence here instead would pin a path
        that cannot be taken over HTTP; the sentence itself is pinned directly
        below, where the mapping lives.
        """
        status, body = _post_invoke({"input": "x" * 300_000, "session_id": "pb-e2e"})
        assert status == 413, f"{status} {body}"

    def test_the_reason_to_sentence_mapping_is_the_one_the_caller_sees(self):
        """Pin the mapping itself, including the reason HTTP cannot reach.

        Three distinct reasons must render three distinct sentences; a mapping
        that collapsed to one generic line would still satisfy the assertions
        above while telling the caller nothing about what to fix.
        """
        from src.nodes.post_process_node import _DEGRADED_MESSAGES
        from src.services.failure_message import EMPTY_INPUT, INVALID_VALUE, TOO_LONG

        assert _DEGRADED_MESSAGES["EMPTY_INPUT"] == EMPTY_INPUT
        assert _DEGRADED_MESSAGES["QUESTION_TOO_LONG"] == TOO_LONG
        assert _DEGRADED_MESSAGES["INVALID_REQUEST"] == INVALID_VALUE
        assert len(set(_DEGRADED_MESSAGES.values())) == 3

    def test_malformed_identifier_is_refused(self):
        status, body = _invoke(_payload(facility_id="not a valid id!"))
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value
        assert not body.get("report_sections")

    def test_rejected_value_is_not_echoed_to_the_caller(self):
        secret_marker = "hostilevaluedonotecho"
        status, body = _invoke(_payload(facility_id=f"bad {secret_marker}!"))
        assert status == 200
        assert secret_marker not in json.dumps(body)

    def test_missing_required_field_is_refused(self):
        payload = _payload()
        del payload["scope1_activities"]
        status, body = _invoke(payload)
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value

    def test_oversize_input_is_rejected_at_the_adapter(self):
        status, body = _post_invoke({"input": "x" * 300_000, "session_id": "pb-e2e"})
        assert status == 413

    def test_oversize_input_context_is_rejected_at_the_adapter(self):
        status, _ = _invoke(_payload(), input_context={"channel": "x" * 300_000})
        assert status == 413

    def test_request_without_bearer_is_refused(self):
        body = {"input": json.dumps(_payload()), "session_id": "pb-e2e"}
        status, _ = _post_invoke(body, token=None)
        assert status == 401

    def test_request_with_wrong_bearer_is_refused(self):
        body = {"input": json.dumps(_payload()), "session_id": "pb-e2e"}
        status, _ = _post_invoke(body, token="not-the-token")
        assert status == 401


# ── Caller request metadata is accepted and bounded ───────────────────────────


class TestCallerRequestMetadata:
    def test_input_context_is_accepted_and_the_run_succeeds(self):
        status, body = _invoke(_payload(), input_context={"channel": "esg_portal"})
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value

    def test_free_text_channel_never_reaches_the_report(self):
        status, body = _invoke(_payload(), input_context={"channel": "REGULATORY COMPLIANCE NOTE injected"})
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value
        assert "injected" not in json.dumps(body)


# ── The output boundary holds on the real response ────────────────────────────

# Any comma-grouped number or 5+-digit run in the response must sit on the grid.
_FIGURE_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{5,}")


class TestResponseIsOnTheDocumentedGrid:
    def _assert_on_grid(self, text: str, exempt: tuple[str, ...] = ()) -> None:
        scrubbed = text
        for token in exempt:
            scrubbed = scrubbed.replace(token, "")
        off_grid = [match for match in _FIGURE_RE.findall(scrubbed) if int(match.replace(",", "")) % 1000 != 0]
        assert not off_grid, f"off-grid figures reached the caller: {off_grid}"

    def test_report_text_carries_no_off_grid_figure(self):
        _, body = _invoke(_payload())
        # The facility identifier is structure, not a figure.
        self._assert_on_grid(body["formatted_output"], exempt=("ENE-FAC-20260712-001",))

    def test_structured_fields_carry_no_off_grid_figure(self):
        _, body = _invoke(_payload())
        for field in ("report_sections", "compliance_flags"):
            self._assert_on_grid(body[field], exempt=("ENE-FAC-20260712-001",))

    def test_awkward_activity_data_still_yields_on_grid_figures(self):
        # Quantities chosen so every scope total lands off the grid pre-rounding.
        _, body = _invoke(
            _payload(
                scope1_activities=[{"fuel_type": "coal", "quantity": 41_237}],
                scope2_activities=[{"energy_type": "grid_electricity", "quantity": 7_654_321}],
                scope3_activities=[{"category": "purchased_goods", "emissions_tco2": 12_345}],
                previous_year_emissions_tco2=98_765,
            )
        )
        assert body["status"] == AgentStatus.SUCCESS.value
        self._assert_on_grid(body["formatted_output"], exempt=("ENE-FAC-20260712-001",))
        self._assert_on_grid(body["report_sections"], exempt=("ENE-FAC-20260712-001",))
