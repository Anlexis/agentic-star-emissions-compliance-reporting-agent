"""AgentCore Platform v1.0"""

# ENE-C2-007 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the emissions compliance report generation pipeline:
#
#   START
#     → input_validate           (InputValidateNode)
#     → parse_emissions_data     (ParseEmissionsDataNode)
#     → generate_report_sections (GenerateReportSectionsNode)
#     → compliance_check         (ComplianceCheckNode)
#     → output_format            (OutputFormatNode)
#     → END
#
# Called by EmissionsReportGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Contracts enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with EmissionsReportGraphNode.merge_output()
#   ✅ All nodes implement execute(self, state) — no config parameter;
#      node config is constructor-injected
#   ✅ _validate_config() rejects malformed generation settings at compile
#      time (fail closed)
#   ❌ No platform-SDK imports

import math
from typing import Any

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.compliance_check_node import ComplianceCheckNode
from src.nodes.generate_report_sections_node import GenerateReportSectionsNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.parse_emissions_data_node import ParseEmissionsDataNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for ENE-C2-007.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by EmissionsReportGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate            (InputValidateNode)
          → parse_emissions_data      (ParseEmissionsDataNode)
          → generate_report_sections  (GenerateReportSectionsNode)
          → compliance_check          (ComplianceCheckNode)
          → output_format             (OutputFormatNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "ene_c2_007_emissions_compliance_report_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate the forwarded runtime settings at compile time.

        The outer graph forwards config/config.yaml's generation settings
        (``configurable``: system prompt template, temperature, max_tokens)
        and ``timeout_s``. All settings are optional, but a PRESENT setting
        must be well-formed — a malformed value raises ConfigError at
        compile time instead of being silently dropped at run time.
        """
        configurable = self.config.get("configurable")
        if configurable is not None:
            if not isinstance(configurable, dict):
                raise ConfigError(f"[{self.__class__.__name__}] 'configurable' must be a dict")
            temperature = configurable.get("temperature")
            if temperature is not None:
                if (
                    isinstance(temperature, bool)
                    or not isinstance(temperature, (int, float))
                    or not math.isfinite(float(temperature))
                    or not (0.0 <= float(temperature) <= 2.0)
                ):
                    raise ConfigError(
                        f"[{self.__class__.__name__}] 'configurable.temperature' "
                        "must be a finite number in [0.0, 2.0]"
                    )
            max_tokens = configurable.get("max_tokens")
            if max_tokens is not None:
                if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not (1 <= max_tokens <= 200_000):
                    raise ConfigError(
                        f"[{self.__class__.__name__}] 'configurable.max_tokens' " "must be an integer in [1, 200000]"
                    )
        timeout_s = self.config.get("timeout_s")
        if timeout_s is not None:
            if (
                isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or not (0 < float(timeout_s) <= 3600)
            ):
                raise ConfigError(f"[{self.__class__.__name__}] 'timeout_s' must be a finite " "number in (0, 3600]")

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        Nodes take no execute() config parameter: configuration is injected
        through the node constructor instead. GenerateReportSectionsNode
        receives this graph's config dict (BaseGraph.__init__ stores the
        dict passed by EmissionsReportGraphNode._parent_config() in
        graph.py — the carrier of the ``configurable`` key). The other
        nodes read no config and stay no-arg.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["parse_emissions_data"] = ParseEmissionsDataNode()
        self._nodes["generate_report_sections"] = GenerateReportSectionsNode(config=self.config)
        self._nodes["compliance_check"] = ComplianceCheckNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear emissions-report generation topology.

        Linear flow:
            input_validate → parse_emissions_data → generate_report_sections
            → compliance_check → output_format → END.

        No conditional branching — all paths through the report pipeline are
        linear in v1.  route() satisfies the ABC but is not used at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "parse_emissions_data")
        self._sg.add_edge("parse_emissions_data", "generate_report_sections")
        self._sg.add_edge("generate_report_sections", "compliance_check")
        self._sg.add_edge("compliance_check", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this method
        is never called at runtime.  Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by EmissionsReportGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "compliance_report", "report_sections",
                                        "compliance_flags", "reporting_required", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "compliance_report": state.get("compliance_report"),
            "report_sections": state.get("report_sections"),
            "compliance_flags": state.get("compliance_flags"),
            "reporting_required": state.get("reporting_required", False),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
