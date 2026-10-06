"""AgentCore Platform v1.0"""

# The manifest (config/agent.yaml) declares the agent class as
# src.graph.graph.EmissionsComplianceReportGeneratorAgent. This package also
# re-exports the class (and the `Graph` alias) so package-level imports keep
# working for callers that resolve via `src.graph`.
from src.graph.graph import EmissionsComplianceReportGeneratorAgent, Graph

__all__ = ["EmissionsComplianceReportGeneratorAgent", "Graph"]
