"""AgentCore Platform v1.0"""

# ENE-C2-007 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, bounded by config max_retry)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (EmissionsReportGraphNode) that delegates
#   the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Runtime configuration:
#   config/config.yaml carries the runtime parameters (max_retry, timeout_s,
#   the llm block, security flags). The platform passes that dict into the
#   Graph constructor; when the agent is constructed bare (tests, the
#   standalone server), __init__ loads the same file itself so declared
#   values never silently degrade to defaults. EmissionsReportGraphNode
#   forwards the values to the inner graph, whose _validate_config()
#   rejects malformed settings at compile time (fail closed).
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Contracts enforced:
#   ✅ EmissionsComplianceReportGeneratorAgent inherits AgentBaseGraph directly
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ EmissionsReportGraphNode assigned to self._nodes["main"]
#   ✅ PreProcessNode (VERIFIED_EXTERNAL) in pre_process slot (trust gate)
#   ✅ PostProcessNode (ANONYMOUS) in post_process slot (output gate)
#   ✅ merge_output() returns only changed keys
#   ✅ get_output() surfaces the domain result on the compiled outer-invoke path
#   ✅ class name matches config/agent.yaml class: field exactly
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _load_runtime_config() -> Dict[str, Any]:
    """Load the runtime parameter file (config/config.yaml).

    Returns {} when the file is absent or unreadable — the framework then
    falls back to its own defaults. The result is validated by
    AgentBaseGraph._validate_config() (outer keys) and
    DomainWorkflowGraph._validate_config() (forwarded generation settings)
    at compile time.
    """
    try:
        with open(_RUNTIME_CONFIG_PATH, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class EmissionsReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of EmissionsComplianceReportGeneratorAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input (trust-gate output) from outer state
      merge_output()  — map sub_result fields into outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Store the outer graph's runtime config for forwarding to the subgraph."""
        self._config: Dict[str, Any] = config if config is not None else {}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates and normalises the raw user_input and writes
        the result to validated_input.  Prefer that; fall back to user_input
        if validated_input is absent (e.g. in unit tests).
        """
        value = state.get("validated_input", state.get("user_input", ""))
        return str(value) if value is not None else ""

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "compliance_report", "report_sections",
                                       "compliance_flags", "reporting_required", "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads these keys from state to
        apply the output gate and set formatted_output.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "compliance_report": sub_result.get("compliance_report"),
            "report_sections": sub_result.get("report_sections"),
            "compliance_flags": sub_result.get("compliance_flags"),
            "reporting_required": sub_result.get("reporting_required", False),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Shape the runtime config forwarded to the inner graph.

        Maps the config/config.yaml ``llm`` block into the ``configurable``
        dict GenerateReportSectionsNode consumes (system prompt template,
        temperature, max_tokens), and forwards ``timeout_s`` for the inner
        graph's config validation. The inner graph rejects malformed values
        at compile time rather than running with silently-dropped settings.
        """
        cfg: Dict[str, Any] = self._config or {}
        raw_llm = cfg.get("llm")
        llm: Dict[str, Any] = raw_llm if isinstance(raw_llm, dict) else {}
        inner: Dict[str, Any] = {
            "configurable": {
                "system_prompt_template": llm.get("system_prompt_template", ""),
                "temperature": llm.get("temperature", 0.0),
                "max_tokens": llm.get("max_tokens", 4000),
            }
        }
        if "timeout_s" in cfg:
            inner["timeout_s"] = cfg["timeout_s"]
        return inner


class EmissionsComplianceReportGeneratorAgent(AgentBaseGraph):
    """Outer graph for ENE-C2-007 (Cat 2 — document-generation pipeline).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    EmissionsReportGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() and get_output() are the only overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode    (VERIFIED_EXTERNAL — trust gate)
      - main:        EmissionsReportGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode  (ANONYMOUS — output gate)
      - get_output(): surfaces the domain result on the compiled outer invoke

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    Class name MUST match config/agent.yaml `class:` field exactly.
    server.py imports this class directly.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Accept a runtime config dict; load config/config.yaml when absent.

        The platform passes the parsed config/config.yaml into the
        constructor. When the agent is constructed bare (tests, the
        standalone server), the same file is loaded here so declared runtime
        values (max_retry, generation settings) actually reach the graph
        instead of silently degrading to framework defaults.
        """
        super().__init__(config=config if config is not None else _load_runtime_config())

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "EmissionsComplianceReportGeneratorAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).

        EmissionsReportGraphNode receives this graph's config dict so the
        values declared in config/config.yaml reach the inner graph.
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = EmissionsReportGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the domain compliance-report result on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope,
        which would drop the structured domain result on the compiled
        outer-graph success path. This override extends the base envelope so a
        successful invocation actually returns the domain result.

        Output-gate invariant preserved (fail-closed):
          * ``formatted_output`` is what the gated PostProcessNode produced —
            on a block it is that gate's own content-free withholding notice —
            so it is the only caller-facing value on either path. It is the
            single field this template writes at the output boundary; nothing
            upstream (``merge_output`` included) may write it, and
            tests/unit/test_output_envelope.py pins that coupling.
          * ``result`` is the PRE-gate value the workflow produced. It is
            surfaced ONLY on the gated success path.
          * The base envelope resolves its ``output`` key as
            ``formatted_output or result`` WITHOUT consulting status. Two
            consequences, both handled here rather than reproduced:
              - that expression is NOT re-implemented in this override. A
                template that repeats it defeats even a future framework-side
                status guard, so the resolution lives in exactly one place.
              - on any non-success outcome the fallback is re-resolved as
                ``formatted_output or None``: an absent gate output stays
                absent and never becomes the pre-gate answer. Without this,
                every refusal the framework itself raises — an exception in
                PostProcessNode, the framework's own credential raise, a subgraph
                error routed straight to finalize — returns a bare error
                partial that clears nothing, and the envelope hands back
                whatever ``result`` still held.
          * ``compliance_report`` (caller-facing) is surfaced ONLY on the gated
            success path, and set to the POST-gate value — not the raw report.
          * The structured domain fields (report_sections / compliance_flags /
            reporting_required) are surfaced ONLY when the gate passed
            (status == SUCCESS). On any non-success outcome — including a
            credential block — they are withheld (None).
        """
        output: Dict[str, Any] = super().get_output(state)  # {output, status, trace_id, correlation_id, node_history}
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return output
        succeeded = state.get("status") == AgentStatus.SUCCESS.value

        # The gate's own output — the only caller-facing value on either path.
        formatted_output = state.get("formatted_output")
        output["formatted_output"] = formatted_output

        if succeeded:
            output["result"] = state.get("result")
            # POST-gate compliance report — never the pre-gate raw state["compliance_report"].
            output["compliance_report"] = formatted_output
            output["report_sections"] = state.get("report_sections")
            output["compliance_flags"] = state.get("compliance_flags")
            output["reporting_required"] = state.get("reporting_required")
        else:
            output["result"] = None
            output["compliance_report"] = None
            output["report_sections"] = None
            output["compliance_flags"] = None
            output["reporting_required"] = None
            # Re-resolve the base envelope's value without the `or result`
            # fallback: on a non-success outcome the gate's output is all the
            # caller may see, and an absent one stays absent.
            output["output"] = formatted_output or None
        return output

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for backward compat (server.py may import Graph).
# Class name EmissionsComplianceReportGeneratorAgent matches config/agent.yaml class: field.
Graph = EmissionsComplianceReportGeneratorAgent
