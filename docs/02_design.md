# Template Design Specification — ENE-C2-007 Emissions Compliance Report Generator

## Position in AgentCore Architecture

| Aspect | Value |
|--------|-------|
| Agent class | EmissionsComplianceReportGeneratorAgent |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | Cat 2 — document-generation pipeline (two-layer nested workflow) |

**Three-layer separation**

- State: flat TypedDict composition (no Pydantic — msgpack incompatible); JSON-serialised strings for all dict/list fields
- Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
- Graph: composition (`register_nodes()` for node substitution; nested inner graph via `GraphNode`)

## Domain Context

Emissions compliance report generator for Japanese reporting entities (energy companies, large industrial emitters, ESG compliance teams). Generates structured, GX-Act / 温対法 aligned emissions compliance report drafts from Scope 1/2/3 activity data submitted by a compliance officer.

**Regulatory basis**:
- 地球温暖化対策の推進に関する法律 (温対法) 算定・報告・公表制度 — specified emitters (>= 3,000 tCO2e/year) must report annually to the competent minister via 経済産業省 / 環境省.
- GX推進法 (2023) / GX-ETS — large emitters (>= 100,000 tCO2e/year) participate in the emissions-trading scheme from FY2026.

## Architecture Overview

### Backbone (outer AgentBaseGraph — fixed 5-node pipeline)

```
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                                         ↓ (retry, max 3)
                                       pre_process
```

### Inner Domain Workflow (DomainWorkflowGraph — linear 5-node pipeline)

```
START → input_validate → parse_emissions_data → generate_report_sections
          → compliance_check → output_format → END
```

### Node Configuration

| Node | Class | File | Trust | Responsibility | Input Keys | Output Keys |
|------|-------|------|-------|---------------|------------|-------------|
| initialize | InitializeNode | framework | — | session init | — | session_id, schema_version |
| pre_process | PreProcessNode | src/nodes/pre_process_node.py | VERIFIED_EXTERNAL | trust enforcement + size cap + JSON validation | user_input, input_context | validated_input, enriched_context — or `error_code` alone when the request is declined |
| main | EmissionsReportGraphNode | src/graph/graph.py | — | delegates to DomainWorkflowGraph; stands down when the request was already declined | validated_input, error_code | compliance_report, report_sections, compliance_flags, reporting_required |
| post_process | PostProcessNode | src/nodes/post_process_node.py | ANONYMOUS | output gate (credential scan, caller-text redaction, precision grid); renders the refusal sentence when the run carries a reason code | compliance_report, report_sections, compliance_flags, error_code | formatted_output, result, report_sections, compliance_flags |
| finalize | FinalizeNode | framework | — | response metadata | — | response_metadata, total_time_ms |
| input_validate (inner) | InputValidateNode | src/nodes/input_validate_node.py | ANONYMOUS | caller-data contract: finite/bounded numerics, inert identifiers, structural caps | validated_input | emissions_data — or `error_code` alone when a field is out of contract |
| parse_emissions_data (inner) | ParseEmissionsDataNode | src/nodes/parse_emissions_data_node.py | ANONYMOUS | Scope 1/2/3 computation + classification | emissions_data | emissions_data (enriched) |
| generate_report_sections (inner) | GenerateReportSectionsNode | src/nodes/generate_report_sections_node.py | ANONYMOUS | render the 5 report sections on the published precision grid (deterministic in v1) | emissions_data | report_sections |
| compliance_check (inner) | ComplianceCheckNode | src/nodes/compliance_check_node.py | ANONYMOUS | 温対法 / GX-ETS reportability determination | emissions_data | compliance_flags, reporting_required |
| output_format (inner) | OutputFormatNode | src/nodes/output_format_node.py | ANONYMOUS | assemble final report + disclaimer | report_sections, compliance_flags | compliance_report, result |

### Data Flow

```
user_input (JSON emissions payload)
    │
    ▼ PreProcessNode (VERIFIED_EXTERNAL trust gate)
validated_input (normalised JSON string)
enriched_context (JSON string)
    │
    ▼ EmissionsReportGraphNode → DomainWorkflowGraph
    │   InputValidateNode          → emissions_data (JSON string)
    │   ParseEmissionsDataNode      → emissions_data (enriched: scope totals, class)
    │   GenerateReportSectionsNode  → report_sections (JSON string)
    │   ComplianceCheckNode         → compliance_flags (JSON string), reporting_required (bool)
    │   OutputFormatNode            → compliance_report (str), result (str)
    ▼ merge_output
compliance_report, report_sections, compliance_flags, reporting_required → outer state
    │
    ▼ PostProcessNode (ANONYMOUS output gate)
formatted_output (gated compliance_report), result, gated report_sections + compliance_flags
```

### State Definition

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| validated_input | NotRequired[Optional[str]] | Normalised emissions JSON string | PreProcessNode |
| enriched_context | NotRequired[Optional[str]] | JSON: {source, channel, facility_id} | PreProcessNode |
| emissions_data | NotRequired[Optional[str]] | JSON: parsed + computed emissions payload | InputValidateNode / ParseEmissionsDataNode |
| report_sections | NotRequired[Optional[str]] | JSON: {section_name: text, ...} × 5 sections | GenerateReportSectionsNode |
| compliance_flags | NotRequired[Optional[str]] | JSON: 温対法 / GX-ETS check result | ComplianceCheckNode |
| reporting_required | NotRequired[Optional[bool]] | True if 温対法 reporting required | ComplianceCheckNode |
| compliance_report | NotRequired[Optional[str]] | Final formatted report text (draft) | OutputFormatNode |
| result | NotRequired[Optional[str]] | Same as compliance_report (backbone convention) | OutputFormatNode / PostProcessNode |
| error_code | Optional[str] | Reason a run completed without carrying out the request (`EMPTY_INPUT` / `QUESTION_TOO_LONG` / `INVALID_REQUEST`); internal — never released in the response envelope | PreProcessNode / InputValidateNode |

**Serialization constraint**: all dict/list-valued fields use JSON-serialised `Optional[str]` — LangGraph checkpoints are msgpack-encoded, and a bare dict/list field corrupts silently. `to_json()` / `from_json()` helpers are defined in `src/schemas/state.py` and used at every producer/consumer boundary — one contract end-to-end.

**Prohibited**: re-declaring `formatted_output` (inherited from AgentState), credentials in State, Pydantic models.

### Input Payload Schema (user_input JSON)

```json
{
  "facility_id": "FAC-2026-00042",
  "company_name": "Example Energy K.K.",
  "reporting_year": 2026,
  "scope1_activities": [
    {"fuel_type": "city_gas", "quantity": 120000, "unit": "m3"},
    {"fuel_type": "diesel", "quantity": 5000, "unit": "L"}
  ],
  "scope2_activities": [
    {"energy_type": "grid_electricity", "quantity": 8500000, "unit": "kWh"}
  ],
  "scope3_activities": [
    {"category": "purchased_goods", "emissions_tco2": 12000}
  ],
  "previous_year_emissions_tco2": 45000,
  "reduction_commitments": [
    "Switch to renewable PPA by 2028",
    "Fleet electrification by 2030"
  ]
}
```

### Output Report Sections (GX-Act / 温対法 format)

1. **Emissions Summary** — Scope 1/2/3 breakdown, total, emitter classification
2. **Calculation Methodology** — GHG Protocol + 環境省 emission factors; data-quality flags
3. **Regulatory Framework** — 温対法 算定・報告・公表制度 + GX推進法 / GX-ETS citations
4. **Year-over-Year Comparison** — change vs prior-year baseline (absolute + %)
5. **Reduction Commitments** — stated reduction commitments toward Carbon Neutrality 2050

The assembled report ends with the mandatory draft disclaimer (final submission requires review by a certified environmental consultant and approval by the compliance officer).

## Security Configuration

| Concern | Gate | Implementation |
|---------|------|---------------|
| Caller trust | Entry-point auth + node trust level | `src/api/server.py` bearer-token check; PreProcessNode `required_trust_level = VERIFIED_EXTERNAL` |
| Input bounds | Structural + domain validation | PreProcessNode (size cap, JSON shape, required fields, inert channel) + InputValidateNode (finite/bounded numerics, inert identifiers, list caps, whitelist-copied records). Every one of these is a value the caller can correct, so the refusal completes the run carrying a reason code — see **Refusing a request** below |
| Output safety | Three-layer output gate + envelope containment | PostProcessNode module-level `_security_gate_output()` (credential scan — the UNION of the domain patterns and the framework's own `detect_credentials()`) + verbatim caller-text redaction + `_enforce_precision()` grid enforcement, applied to every outgoing representation. A block CLEARS every caller-facing field (`_CLEARED_ON_VIOLATION`) with a TRUTHY withholding notice, and `get_output()` surfaces `result` only on the gated success path — see **Output-gate containment** below |
| Audit | Trace events | `emit_trace_event()` in every node's `execute()` (at least one domain-specific event), plus an event per redaction |
| Credentials | Never in State | No credential-shaped fields in State; secrets resolved through the invocation context only |

**config/config.yaml** (runtime parameters; `config/agent.yaml` is the flat manifest and carries
identity + compile-time requirements only):
```yaml
max_retry: 3
timeout_s: 30
llm:
  system_prompt_template: "prompts/emissions_report.j2"
  temperature: 0.0
  max_tokens: 6000
security:
  s3_gate_enabled: true
```

`temperature: 0.0` is mandatory — the report is a regulatory artifact and must be deterministic.
The outer graph loads this file (`_load_runtime_config()`), the framework validates `max_retry`,
and `EmissionsReportGraphNode._parent_config()` forwards the generation settings and `timeout_s`
into the inner graph, whose `_validate_config()` rejects a malformed value at compile time.

### Refusing a request

A refusal never processes the request: no emissions figure is computed, no
section is rendered, no report is assembled, and the reason is written to
`error_log` and to an audit event exactly as before. What differs between the
two classes of refusal is how the outcome is reported, and that follows from
whether the caller can act on it.

**A reason the caller can correct completes the run.** An empty request, a body
over the size cap, a payload that is not parseable JSON or not a JSON object, a
missing required field, and every out-of-contract value the caller-data contract
catches (`facility_id`, `company_name`, `reporting_year`, the activity lists and
their records, `previous_year_emissions_tco2`, `reduction_commitments`) are all
values the caller can fix. The run finishes with `status = success` carrying a
reason code in State — `EMPTY_INPUT`, `QUESTION_TOO_LONG` for the size cap, and
`INVALID_REQUEST` for the rest — and the caller-facing body is the fixed sentence
for that code (`src/services/failure_message.py`). Terminating instead would end
the calling surface's turn and surface an exception type alone, leaving the
reason reachable only from the audit trail; completing lets the compliance
officer correct the payload and send it again on the same conversation.

**A reason the caller cannot correct terminates the run.** The output gate's own
refusal (a credential shape, verbatim caller text) and a broken internal
invariant — `emissions_data` absent when `parse` / `generate` / `compliance_check`
runs, `report_sections` absent at `output_format` — keep `status = error`. These
are not values a caller can restate; each means the pipeline itself is in a state
it promised could not occur.

**The reason code stays internal.** `error_code` is a State field and is never
released in the response envelope. The caller is told what to correct by the
sentence, never by a code.

**Everything after the mark stands down.** Once `error_code` is set, the
`GraphNode` skips the inner workflow entirely and each remaining node returns the
mark untouched, so no node computes on a payload that was already declined.
`post_process` renders the sentence as `formatted_output`, and `get_output()`
returns the base envelope only: a run that did not carry out the request releases
no `result`, no `compliance_report` and no structured domain field. The mark
crosses the inner/outer boundary in both directions, and a reason settled on the
outer backbone wins over one the inner graph would otherwise overwrite it with.

### Output-gate containment

`AgentBaseGraph.get_output()` resolves the caller-facing value as
`state["formatted_output"] or state["result"]` with **no status check**, and `result` is the
PRE-gate value the workflow produced. An output gate that returns `AgentStatus.ERROR` without
clearing state therefore ships the refused report inside the error envelope. Three framework
properties make this sharper than it looks:

1. a **falsy** `formatted_output` (`""`, `{}`, absent) does not suppress that fallback — it
   **activates** it;
2. `BaseNode.__call__` converts an exception inside a node into a bare ERROR partial that clears
   nothing, so refusals the framework itself raises never reach the gate's clearing at all;
3. `FunctionNode._security_gate_output` is `@final`, **raises** when a credential appears in any
   returned value, and the wrapper then discards the node's whole delta — the gate's clearing
   included.

Both halves of the contract are therefore implemented, because either alone is insufficient:

- **At the gate** (`src/nodes/post_process_node.py`). A violation returns `_CLEARED_ON_VIOLATION`:
  `formatted_output` becomes a **truthy**, content-free withholding notice (a falsy one would
  activate the fallback), and `result`, `compliance_report`, `report_sections`, `compliance_flags`
  and `reporting_required` are all overwritten. Violation messages name the **location** and the
  detector class only — never the matched value, which would re-enter this node's own return and
  trip property 3. Recognition is the union of the domain patterns and the framework's
  `detect_credentials()`: a shape the framework catches and the domain gate misses is a containment
  *bypass* (property 3), not merely a narrower gate. Four such shapes had no domain equivalent:
  `sk_live_`/`sk_test_`, `AKIA…`, a dotless `eyJ…` JWT, and `postgresql://`-style connection strings.
- **At the envelope** (`src/graph/graph.py`). `get_output()` no longer re-implements
  `formatted_output or result` — a template that repeats the framework's un-guarded resolution
  defeats even a future framework-side status guard. `result` and every structured domain field are
  surfaced **only** when `status == SUCCESS`; on any non-success outcome `output` is re-resolved as
  `formatted_output or None`, so an absent gate output stays absent. A run carrying a reason code is
  withheld ahead of that status test: the status is SUCCESS, but the request was never carried out,
  so none of those fields exists and the base envelope is returned unextended. This is the only containment on
  the paths of properties 2 and 3, and on a terminal inner status (`TIMEOUT`/`CANCELLED`) that
  `AgentBaseGraph.route()` sends straight to finalize with the gate skipped.

`formatted_output` is surfaced on both paths because the output gate is its only writer — on a block
it is that gate's own notice. That coupling is pinned by
`tests/unit/test_output_envelope.py::test_merge_output_does_not_write_the_gate_owned_fields`, and the
`_CLEARED_ON_VIOLATION` inventory is cross-checked against the envelope's own key set so a domain
field added to `get_output()` later cannot quietly stay out of the clearing.

**Reachability with the code as shipped:** not reachable through `POST /invoke`. Measured — caller
data cannot drive it: every inner node is a `FunctionNode`, so a framework-known credential shape in
caller text is refused one node earlier (`node_history` stops at `EmissionsReportGraphNode`), and the
domain gate's own refusal path did overwrite `formatted_output`/`result`. The exposure is structural:
`GraphNode._security_gate_output` is a deliberate framework no-op, so anything `merge_output` writes
reaches outer state unscanned, and the envelope surfaced `result` unconditionally.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (`config["configurable"]` — session_id, trust_level)
- [x] Module-level `_security_gate_output()` in `post_process_node.py` — credential/secret scan on every outgoing representation
- [x] `emit_trace_event()` — at least one domain-specific event per node `execute()`
- [x] `to_json()` / `from_json()` helpers in `src/schemas/state.py` — the serialisation contract

### Composition Pattern

- **Pattern**: Cat 2 nested two-layer — GraphNode wrapping inner BaseGraph
- **Outer graph**: `EmissionsComplianceReportGeneratorAgent(AgentBaseGraph)` — fixed 5-node backbone
- **Inner graph**: `DomainWorkflowGraph(BaseGraph)` — 5-node linear domain pipeline
- **Error propagation**: propagate (SubgraphError on inner failure; outer backbone retries pre_process). A caller-correctable refusal is not an inner failure: it is settled before the subgraph runs and the subgraph is skipped (**Refusing a request**)

## Import Isolation Confirmation
- [x] Template does not import the platform-internal SDK
- [x] Import targets: `framework/` and `shared/` only

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed sequential pipeline; no autonomous reasoning loop required |
| Composition pattern | Flat single-slot | Nested GraphNode | Nested | 5 sequential domain steps behind one backbone slot |
| 温対法 compliance | Inline in generate | Separate ComplianceCheckNode | Separate node | Single responsibility: rendering and the regulatory determination change for different reasons |
| State dict fields | bare dict | JSON-serialised str | JSON-serialised str | Checkpoint (msgpack) serialisation safety |
| LLM integration | Real LLM | Deterministic synthesis | Deterministic synthesis (v1) | A regulatory artifact must be reproducible; the prompt template and generation settings ship ready for production LLM wiring |
| Emission computation | In InputValidate | Separate ParseEmissionsDataNode | Separate node | Keeps validation and factor-based computation single-responsibility |
| Reporting a caller-correctable refusal | Terminate the run | Complete the run carrying a reason code | Complete | Terminating ends the calling surface's turn and shows an exception type only, leaving the reason reachable from the audit trail alone; completing tells the compliance officer which field to correct and lets them resend on the same conversation. The refusal itself is unchanged — nothing is computed and no product is released |
| Reason code in the response envelope | Publish it alongside the message | Keep it in State, publish the sentence only | State only | A code is an internal routing label; what the caller needs is the sentence naming what to correct, and publishing both invites callers to branch on a label the template is then never free to change |
