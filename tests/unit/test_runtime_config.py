# ENE-C2-007 — Runtime configuration reaches the graph that consumes it
#
# The runtime parameters live in config/config.yaml (max_retry, timeout_s, the
# generation block). A config reader pointed at a file or key that no longer
# exists returns {} silently, and the declared values never reach the graph —
# a defect that green tests hide. These tests assert the values travel
# end-to-end: file -> outer graph -> GraphNode -> inner graph -> node, and
# that a malformed value fails CLOSED at compile time.

from pathlib import Path

import pytest
import yaml

from framework.errors import ConfigError
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import (
    EmissionsComplianceReportGeneratorAgent,
    EmissionsReportGraphNode,
    _load_runtime_config,
)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class TestRuntimeConfigIsLoaded:
    def test_config_file_exists_and_declares_the_runtime_keys(self):
        assert _CONFIG_PATH.exists(), "config/config.yaml carries the runtime parameters"
        declared = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
        assert declared["max_retry"] == 3
        assert declared["timeout_s"] == 30
        assert declared["llm"]["temperature"] == 0.0

    def test_loader_returns_the_declared_values(self):
        loaded = _load_runtime_config()
        assert loaded["max_retry"] == 3
        assert loaded["timeout_s"] == 30
        assert loaded["llm"]["system_prompt_template"] == "prompts/emissions_report.j2"

    def test_bare_construction_still_carries_the_declared_values(self):
        # A bare Graph() (tests, the standalone server) must not silently fall
        # back to framework defaults.
        agent = EmissionsComplianceReportGeneratorAgent()
        assert agent.config.get("max_retry") == 3
        assert agent.config.get("timeout_s") == 30

    def test_explicit_config_wins_over_the_file(self):
        agent = EmissionsComplianceReportGeneratorAgent(config={"max_retry": 1})
        assert agent.config == {"max_retry": 1}


class TestConfigReachesTheInnerGraph:
    def test_main_slot_node_receives_the_graph_config(self):
        agent = EmissionsComplianceReportGeneratorAgent()
        agent.compile()
        main_node = agent._nodes["main"]
        assert isinstance(main_node, EmissionsReportGraphNode)
        assert main_node._config.get("max_retry") == 3

    def test_generation_settings_are_mapped_for_the_inner_graph(self):
        node = EmissionsReportGraphNode(config=_load_runtime_config())
        forwarded = node._parent_config()
        configurable = forwarded["configurable"]
        assert configurable["system_prompt_template"] == "prompts/emissions_report.j2"
        assert configurable["temperature"] == 0.0
        assert configurable["max_tokens"] == 6000
        assert forwarded["timeout_s"] == 30

    def test_settings_reach_the_section_generator_node(self):
        node = EmissionsReportGraphNode(config=_load_runtime_config())
        inner = node.get_subgraph()
        inner.register_nodes()
        generator = inner._nodes["generate_report_sections"]
        assert generator._config["configurable"]["max_tokens"] == 6000

    def test_declared_values_survive_the_whole_chain(self):
        # file -> outer graph -> GraphNode -> inner graph -> node
        agent = EmissionsComplianceReportGeneratorAgent()
        agent.compile()
        inner = agent._nodes["main"].get_subgraph()
        assert inner.config["configurable"]["temperature"] == 0.0
        assert inner.config["timeout_s"] == 30


class TestMalformedConfigFailsClosed:
    @pytest.mark.parametrize(
        "bad_config",
        [
            {"configurable": "not-a-dict"},
            {"configurable": {"temperature": float("nan")}},
            {"configurable": {"temperature": 9.0}},
            {"configurable": {"temperature": True}},
            {"configurable": {"max_tokens": 0}},
            {"configurable": {"max_tokens": True}},
            {"configurable": {"max_tokens": "4000"}},
            {"timeout_s": float("inf")},
            {"timeout_s": 0},
            {"timeout_s": -5},
            {"timeout_s": True},
        ],
    )
    def test_inner_graph_rejects_malformed_settings_at_compile_time(self, bad_config):
        graph = DomainWorkflowGraph(config=bad_config)
        with pytest.raises(ConfigError):
            graph.compile()

    def test_outer_graph_rejects_a_malformed_retry_ceiling(self):
        agent = EmissionsComplianceReportGeneratorAgent(config={"max_retry": -1})
        with pytest.raises(ConfigError):
            agent.compile()

    def test_absent_settings_are_accepted(self):
        DomainWorkflowGraph(config={}).compile()
