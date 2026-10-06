# Emissions Compliance Reporting Agent

AI agent for generating emissions compliance reports, built with Agentic Star.

> **Category**: Cat 2 (domain pipeline — document generation)
> **Industry**: Energy
> **Template ID**: ENE-C2-007

## Overview

Turns a facility's Scope 1/2/3 activity data into a draft greenhouse-gas compliance report for Japanese reporting entities.

Given a JSON payload of fuel, purchased-energy and value-chain activity records, the agent applies published national emission factors, computes each scope total and the facility's overall tCO2e figure, classifies the emitter against the statutory thresholds (the 3,000 tCO2e/year annual-report threshold under the Act on Promotion of Global Warming Countermeasures, and the 100,000 tCO2e/year emissions-trading threshold under the GX Promotion Act), and renders a five-section report: emissions summary, calculation methodology, regulatory framework, year-over-year comparison and reduction commitments.

The output is explicitly a **draft** for a compliance officer to review — every report carries the sign-off disclaimer, and a result computed with any non-authoritative emission factor is marked not-final-ready. Emissions figures are published as aggregates rounded to the nearest 1,000 tCO2e; individual activity records are never reproduced in the report.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design specification and the test specification.

## Customising

1. Adjust `config/config.yaml` for your own runtime settings.
2. Replace the emission-factor tables in `src/nodes/parse_emissions_data_node.py` with the
   coefficient dataset your jurisdiction publishes, and update the dataset version recorded
   alongside them.
3. Adjust the statutory thresholds and the regulatory citations in
   `src/nodes/compliance_check_node.py` and `src/nodes/generate_report_sections_node.py`.
4. Review the caller-input bounds in `src/nodes/input_validate_node.py` and the report's
   published precision in `src/nodes/precision_grid.py`.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
