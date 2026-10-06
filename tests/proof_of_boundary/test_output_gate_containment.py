# PB-10: OUTPUT-GATE CONTAINMENT on the real POST /invoke surface
#
# `AgentBaseGraph.get_output()` resolves the caller-facing value as
# `state["formatted_output"] or state["result"]` with NO status check, and
# `result` is the PRE-gate value the workflow produced. Three properties make an
# output gate that only flips the status insufficient:
#
#   1. a FALSY `formatted_output` ("" / {} / absent) does not suppress that
#      fallback, it ACTIVATES it;
#   2. `BaseNode.__call__` turns an exception inside a node into a bare ERROR
#      partial that clears NOTHING — so a gate that raises leaks too, and so
#      does any refusal that never reaches the gate;
#   3. `FunctionNode._security_gate_output` is @final and RAISES when a
#      credential appears in ANY returned value, and the wrapper then discards
#      the node's whole delta — the gate's own clearing included.
#
# Every fault below is injected on the DATA path, never on the gate:
#   * caller text carrying a shape the DOMAIN gate refuses (no patching at all);
#   * a drifted `merge_output` — the documented coupling point between
#     `DomainWorkflowGraph.get_output()` and `EmissionsReportGraphNode`
#     (graph.py:116-136). `GraphNode._security_gate_output` is a deliberate
#     framework no-op, so whatever `merge_output` writes lands in outer state
#     unscanned; the outer PostProcessNode is the only gate standing.
#
# The clean-path control runs first on purpose: a refuse-everything gate would
# pass every containment assertion here, so the same request must be shown to
# produce the real report when nothing is wrong with it.

import asyncio
import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus
from src.api.server import app

_TOKEN = "pb-containment-token"

# The pre-gate answer a drifted merge_output hands to the outer state. Any
# appearance of this in an error envelope is the leak.
_PRE_GATE_ANSWER = "PRE-GATE ANSWER TEXT: Tokyo Bay Thermal total 135,000 tCO2e"

# `api_key=<value>` is refused by the DOMAIN gate and is NOT a shape the
# framework's detect_credentials() models — so it survives every framework scan
# in the inner pipeline and reaches the outer output gate as ordinary report
# text. That makes the domain gate, not the framework, the component under test.
_DOMAIN_ONLY_SECRET = "api_key=ZmFrZS1zZWNyZXQtdmFsdWU"

# A shape the FRAMEWORK refuses but the shipped domain pattern list did not:
# without detector parity the framework raises inside PostProcessNode and
# discards the gate's clearing (property 3 above).
_FRAMEWORK_ONLY_SECRET = "AKIAQ7ZP3XM2VDKL9RTN"


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


def _post_invoke(body_obj: dict) -> tuple[int, dict]:
    """POST /invoke through the real ASGI app (Bearer auth)."""
    body = json.dumps(body_obj).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"authorization", f"Bearer {_TOKEN}".encode()),
    ]
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


def _invoke(emissions: dict) -> tuple[int, dict]:
    return _post_invoke({"input": json.dumps(emissions), "session_id": "pb-containment"})


@pytest.fixture(autouse=True)
def deploy_shaped_env(monkeypatch):
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


def _drift_merge_output(monkeypatch, **overrides):
    """Drift the inner-graph -> outer-state coupling on the DATA path.

    `EmissionsReportGraphNode.merge_output()` and `DomainWorkflowGraph
    .get_output()` are documented as designed together; a change on either side
    that starts forwarding an extra key is an ordinary refactor, and
    GraphNode's output gate is a framework no-op, so nothing scans what arrives.
    The gate, the node, the envelope and every other layer run exactly as
    shipped.
    """
    import src.graph.graph as graph_module

    original = graph_module.EmissionsReportGraphNode.merge_output

    def drifted(self, state, sub_result):
        merged = dict(original(self, state, sub_result))
        merged.update(overrides)
        return merged

    monkeypatch.setattr(graph_module.EmissionsReportGraphNode, "merge_output", drifted)


def _assert_nothing_pre_gate(body: dict) -> str:
    """Common containment assertions for every non-success envelope."""
    blob = json.dumps(body, ensure_ascii=False)

    assert body["status"] == AgentStatus.ERROR.value
    assert body["result"] is None, "the PRE-gate value was surfaced on an error envelope"
    for key in ("compliance_report", "report_sections", "compliance_flags", "reporting_required"):
        assert body[key] is None, f"{key} released on the error path"

    # No released report text, anywhere in the body.
    assert _PRE_GATE_ANSWER not in blob
    assert "EMISSIONS COMPLIANCE REPORT" not in blob
    assert "REGULATORY COMPLIANCE NOTE" not in blob
    assert "135,000" not in blob, "an emissions figure was released"
    assert re.search(r"\d{1,3}(?:,\d{3})+\s*tCO2e", blob) is None, "an emissions figure was released"

    # No refused caller content, no traceback, no source path (a violation
    # message that quoted the refused value would trip the framework's own scan
    # on this very result, and the framework replaces a raising node's return
    # with a traceback — discarding the containment along with it).
    assert _DOMAIN_ONLY_SECRET not in blob
    assert _FRAMEWORK_ONLY_SECRET not in blob
    assert "Traceback" not in blob
    assert "src/nodes/" not in blob
    assert ".py" not in blob
    return blob


class TestCleanPathControl:
    """Without a control, every containment assertion below would also pass on
    an agent that refuses everything."""

    def test_the_same_request_really_produces_the_report(self):
        status, body = _invoke(_payload())
        assert status == 200
        assert body["status"] == AgentStatus.SUCCESS.value, body.get("error_log")
        assert "PostProcessNode" in body["node_history"]
        assert "EMISSIONS COMPLIANCE REPORT" in body["formatted_output"]
        assert body["compliance_report"] == body["formatted_output"]
        assert body["result"]
        assert body["report_sections"] and body["compliance_flags"]
        assert body["reporting_required"] is True
        assert "135,000" in body["output"]


class TestDomainGateBlockIsContained:
    """The gate's own refusal path, driven with NO patching at all."""

    def test_blocked_response_releases_nothing(self):
        status, body = _invoke(_payload(company_name=f"Tokyo Bay {_DOMAIN_ONLY_SECRET} KK"))
        assert status == 200
        # The block happened AT the output gate, not upstream — the request
        # reached post_process and was refused there.
        assert "PostProcessNode" in body["node_history"]
        blob = _assert_nothing_pre_gate(body)

        # What the caller does get: the gate's own truthy, content-free notice,
        # and it is what the envelope's `formatted_output or result` resolves to.
        assert body["formatted_output"], "a falsy replacement ACTIVATES the fallback"
        assert "REDACTED" in body["formatted_output"]
        assert body["output"] == body["formatted_output"]
        # The notice names no detector class and no caller content.
        assert "api_key" not in blob


class TestFrameworkRefusalsAreContained:
    """Refusals the FRAMEWORK raises never reach the gate's clearing.

    `BaseNode.__call__` replaces the node's whole delta with a bare ERROR
    partial, so containment on these paths can only come from the envelope.
    """

    def test_bare_error_inside_post_process_releases_nothing(self, monkeypatch):
        """A drifted merge_output that also forwards `result` (the inner graph
        already emits keys beyond the documented set) plus a shape that makes
        PostProcessNode raise: the node's return — clearing included — is
        discarded, and `result` is left holding the pre-gate answer."""
        _drift_merge_output(
            monkeypatch,
            result=_PRE_GATE_ANSWER,
            # A structured section map where the renderer's text is expected:
            # `compliance_report.strip()` raises, so the node returns nothing.
            compliance_report={"emissions_summary": _PRE_GATE_ANSWER},
        )
        status, body = _invoke(_payload())
        assert status == 200
        assert "PostProcessNode" in body["node_history"]
        _assert_nothing_pre_gate(body)
        assert not body["output"], "the `formatted_output or result` fallback re-opened the path"

    def test_terminal_status_that_skips_the_gate_releases_nothing(self, monkeypatch):
        """A non-success inner outcome that is not ERROR routes straight to
        finalize, so the output gate never runs at all — only the envelope can
        contain what merge_output left in state.

        TIMEOUT rather than ERROR on purpose: `error_strategy="propagate"`
        makes GraphNode RAISE on an inner ERROR before merge_output is reached,
        so ERROR cannot populate outer state. Every other terminal status
        returns normally, merges, and is routed to finalize by
        AgentBaseGraph.route().
        """
        _drift_merge_output(monkeypatch, status=AgentStatus.TIMEOUT.value, result=_PRE_GATE_ANSWER)
        status, body = _invoke(_payload())
        assert status == 200
        assert body["status"] == AgentStatus.TIMEOUT.value
        assert "PostProcessNode" not in body["node_history"], "expected the gate to be skipped"
        assert body["result"] is None, "the PRE-gate value was surfaced on a non-success envelope"
        assert not body["output"], "the `formatted_output or result` fallback re-opened the path"
        blob = json.dumps(body, ensure_ascii=False)
        assert _PRE_GATE_ANSWER not in blob
        assert "135,000" not in blob
        for key in ("compliance_report", "report_sections", "compliance_flags", "reporting_required"):
            assert body[key] is None, f"{key} released on the non-success path"


class TestDetectorParity:
    """A domain pattern set narrower than the framework's is itself a bypass.

    The framework scans every node result with `detect_credentials()` and
    RAISES on a hit; the raise discards the gate's clearing. So a shape the
    framework refuses must be refused by the DOMAIN gate first — visible here
    as the gate's own withholding notice rather than an empty error envelope.
    """

    def test_framework_known_shape_is_refused_by_the_domain_gate(self, monkeypatch):
        _drift_merge_output(
            monkeypatch,
            compliance_report=(
                "EMISSIONS COMPLIANCE REPORT\nScope 2 factor provenance token " f"{_FRAMEWORK_ONLY_SECRET}\n"
            ),
        )
        status, body = _invoke(_payload())
        assert status == 200
        assert "PostProcessNode" in body["node_history"]
        blob = _assert_nothing_pre_gate(body)
        assert body["formatted_output"], "the framework raised first and discarded the gate's clearing"
        assert "REDACTED" in body["formatted_output"]
        assert body["output"] == body["formatted_output"]
        assert "AKIA" not in blob
