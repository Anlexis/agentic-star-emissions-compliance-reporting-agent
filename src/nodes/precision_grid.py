"""AgentCore Platform v1.0"""

# ENE-C2-007 — external emissions-figure precision grid.
#
# The published report schema expresses every emissions figure as an AGGREGATE
# rounded to the nearest 1,000 tCO2e. Individual activity records (fuel
# quantities, per-source factors, provenance text) are inputs to the
# computation and are never rendered on the external surface.
#
# Two cooperating halves share this module:
#   - the section renderers (generate_report_sections_node, compliance_check
#     _node, output_format_node) RENDER on the grid via to_grid()/format_grid();
#   - the output gate (post_process_node) independently ENFORCES the grid on
#     every outgoing representation, so a rendering regression cannot leak a
#     full-precision figure.

import math

GRID_UNIT_TCO2E = 1000

# Schema note printed verbatim in every generated report (and asserted by the
# output-boundary tests): the external precision contract, stated where the
# reader of the report can see it.
SCHEMA_NOTE = (
    "All emissions figures in this report are aggregates rounded to the "
    "nearest 1,000 tCO2e; individual activity records are not reproduced."
)


def to_grid(value: float) -> int:
    """Round an emissions aggregate onto the external grid (nearest 1,000).

    Defense in depth: a non-finite aggregate must never be rendered. Caller
    numerics are validated finite upstream, so reaching here with NaN or an
    infinity means an internal computation went wrong — raise rather than
    emit a meaningless figure (int(float("nan")) would raise anyway, with a
    message that explains nothing).
    """
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("emissions aggregate is not finite; refusing to render a figure")
    return int(round(numeric / GRID_UNIT_TCO2E) * GRID_UNIT_TCO2E)


def format_grid(value: float) -> str:
    """Render an emissions aggregate as a grid-rounded, comma-grouped figure."""
    return f"{to_grid(value):,d}"
