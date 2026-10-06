# Test Specification — ENE-C2-007 Emissions Compliance Report Generator

## 1. Test Strategy

- **Agent:** ENE-C2-007 — Emissions Compliance Report Generator (Cat 2,
  document-generation pattern, two-layer nested graph: outer `AgentBaseGraph`
  backbone + inner `DomainWorkflowGraph` `BaseGraph`).
- **Coverage target:** ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types:** Unit (per node, the output boundary, runtime configuration) ·
  Proof-of-Boundary (framework security/serialization contracts, backbone
  invoke order, end-to-end HTTP invoke) · Integration (compiled outer graph).
- **Framework provisioning:** `framework` (`agenticstar-agentcore`) is installed
  from the package index. Tests import the real modules; there are no stub nodes.
- **Refusal contract in assertions:** a refusal the caller can correct (empty or
  oversized request, unparseable payload, any out-of-contract field) is asserted
  as `status = success` with the request NOT carried out and `error_log`
  populated; a refusal the caller cannot correct (output-gate violation, a broken
  internal invariant) is asserted as `status = error`. The two are never
  interchanged — reading a terminated run as a completed one, or the reverse, is
  precisely what these assertions exist to catch.
- **Audit events:** `emit_trace_event` is patched at the node module level in
  unit tests to avoid audit-backend calls, never via a `sys.modules` stub (which
  would break the real `shared` package the framework loads at import time).

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All 7 domain/backbone nodes + outer & inner graph wiring + the caller-input contract |
| `tests/unit/test_output_precision_gate.py` | The output boundary: precision-grid grammar (both directions) + the three gate layers |
| `tests/unit/test_runtime_config.py` | `config/config.yaml` reaches the graph that consumes it; malformed settings fail closed |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06/TC-07 — the framework input/output gates cannot be overridden |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + trust gate + payload alignment |
| `tests/proof_of_boundary/test_invoke_e2e.py` | End-to-end business behaviour through `POST /invoke` (bearer auth, real compiled agent) |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 platform-SDK import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State msgpack/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 HITL interrupt-propagation (skip stub — no cross-boundary HITL) |
| `tests/integration/test_outer_invoke_returns_domain_result.py` | The compiled outer graph surfaces the domain result; the output gate withholds it on a violation |

### Canonical valid payload (PB-6 `_VALID_PAYLOAD`)

The large-emitter facility used by the backbone invoke test and by
`deploy/invoke_payload.json` (the two MUST stay identical — asserted by
`test_invoke_payload_matches_pb6`):

```json
{
  "facility_id": "ENE-FAC-20260712-001",
  "company_name": "Tokyo Bay Thermal Power K.K.",
  "reporting_year": 2025,
  "scope1_activities": [
    {"fuel_type": "coal", "quantity": 50000},
    {"fuel_type": "city_gas", "quantity": 1200000}
  ],
  "scope2_activities": [
    {"energy_type": "grid_electricity", "quantity": 8000000}
  ],
  "scope3_activities": [
    {"category": "purchased_goods_and_services", "emissions_tco2": 15000}
  ],
  "previous_year_emissions_tco2": 145000,
  "reduction_commitments": ["Reduce Scope 1+2 emissions 46% by FY2030 ...", "Achieve carbon neutrality by FY2050."]
}
```

Total emissions ≈ 137,720 tCO2e ⇒ ≥ 100,000 tCO2e ⇒ `reporting_required = True`
(温対法 特定排出者) **and** `gx_ets_applicable = True` (GX-ETS large emitter);
`emitter_class = large_emitter_gx_ets`.

## 2. Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Invalid/empty/non-JSON input rejected at PreProcessNode | the refusal COMPLETES the run (`status=success`) with `error_log` naming the problem, so the caller can correct the payload and resend on the same conversation | `TestPreProcessNode` |
| TC-03 | No JWT/credential in State | CI `gate-credential-scan`: 0 violations | CI + `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract — no `_invoke_impl` | Signature `(self, state)`, `_invoke_impl` absent | `test_execute_signature_is_state_first`, `test_main_node.py` |
| TC-05 | Audit: `emit_trace_event()` called inside each node `execute()` | ≥1 domain event per node (positional form) | source inspection |
| TC-08 | Trust: `required_trust_level` enforced in `__call__` before `execute()` | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestTrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | Output gate on post_process | credential pattern → redacted + `status=error`; clean → pass | `TestPostProcessNode`, `TestOutputGateLayers` |
| TC-11a | Output-gate CONTAINMENT — a block clears every caller-facing field, not just the status | `_CLEARED_ON_VIOLATION` applied in full; `formatted_output` replacement TRUTHY so the envelope's `formatted_output or result` fallback cannot resolve to the pre-gate report; violation names the location + detector class, never the value | `TestPostProcessNode::test_gate_redacts_credential_leak` |
| TC-11b | Detector PARITY with the framework recognizer | every shape `detect_credentials()` refuses is refused by the domain gate first — otherwise the framework's `@final` output gate raises and DISCARDS the clearing | `TestDetectorParity`, `test_helper_detects_and_clears` |
| TC-11c | Outer `invoke()` envelope (`get_output`) | success surfaces the gated output + structured keys; on ANY non-success `result` and every structured field are `None` and `output` is re-resolved as `formatted_output or None`; the framework fallback is not re-implemented in template code; the containment inventory matches the envelope's own key set | `tests/unit/test_output_envelope.py` |
| TC-11d | Envelope on a declined-but-correctable run | `status` is `success` and the structured product is ABSENT from the envelope — `compliance_report` / `compliance_flags` / `report_sections` are not present-and-`None`, they are not there at all, because nothing was produced. The caller-facing sentence is asserted too: see TC-11e — a declined run's whole payload to the caller is that one line, so leaving it untested would let the run render the wrong reason, or nothing at all, with every other assertion still passing. | `TestValidationRejectionThroughInvoke` |
| TC-11e | The sentence a declined run renders, per reason | `EMPTY_INPUT` → "No question was received…"; `INVALID_REQUEST` → "A value in the request could not be accepted…". Both asserted through `POST /invoke`, with the structured product still absent | `TestValidationRejectionThroughInvoke` |
| TC-11f | A request past the 256 KiB cap | **413 at the adapter**, not the node's too-long sentence. The adapter and the node cap at the same value, so an oversized body never reaches the graph — the sentence is unreachable over HTTP and is pinned at its mapping instead (TC-11g) | `TestValidationRejectionThroughInvoke` |
| TC-11g | Reason → sentence mapping | All three reasons map to three **distinct** sentences. A mapping collapsed to one generic line would satisfy TC-11e while telling the caller nothing about what to fix | `TestValidationRejectionThroughInvoke` |
| TC-12 | Output precision grid enforced for every representation | off-grid figure snapped + audited; on-grid + structural tokens byte-identical | `TestPrecisionGridSnapsEveryForm` |
| TC-13 | Caller numerics finite + bounded, failing closed | NaN / ±Infinity / negative / over-magnitude → the run completes (`status=success`) with `error_log` naming the field and never echoing the value; nothing is computed from the declined payload | `TestInputValidateNumericGuards` |
| TC-14 | Caller strings inert or bounded; structure capped | free text / control chars / over-length / over-cap lists → the run completes (`status=success`) with `error_log` naming the field | `TestCallerStringAndStructureContract` |
| TC-15 | Runtime configuration reaches the inner graph; malformed values fail closed | declared values observable end-to-end; bad value → `ConfigError` at compile | `tests/unit/test_runtime_config.py` |

## 3. Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 Level-0 (`agenticstar` / platform) imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: trust gate → node_start audit → input gate → `execute()` → output gate → node_complete audit | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, EmissionsReportGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end-to-end | `TestBackboneInvokeOrder` |
| PB-6d | Payload alignment | `deploy/invoke_payload.json["input"] == _VALID_PAYLOAD` | the deployment smoke invoke exercises the PB-6 payload | `test_invoke_payload_matches_pb6` |
| PB-6e | Trust denial (node boundary) | bare ANONYMOUS call to PreProcessNode | `status=error`, "trust gate" in error_log (graph-level ANONYMOUS may still SUCCEED via InitializeNode) | `test_pre_process_denies_anonymous_caller` |
| PB-8 | End-to-end HTTP invoke | `POST /invoke` with bearer auth against the real compiled agent | real report from caller data; every regulatory outcome reachable; a declined payload completes (`status=success`) with the structured product absent from the envelope, so nothing is released for a request that was never carried out; response on the published grid | `tests/proof_of_boundary/test_invoke_e2e.py` |
| PB-10 | Output-gate containment on the real `/invoke` surface | clean-path control (the same request DOES produce the report, `PostProcessNode` in `node_history`); then four non-success paths — a domain-gate block driven by caller text alone, a bare ERROR inside `post_process`, a terminal inner status that skips the gate, and a framework-known credential shape — each measured on the real envelope | error envelope carries no report header, no emissions figure, no pre-gate answer, no refused caller content, no traceback and no source path; `result` and every structured field `None`; `output` is the gate's own withholding notice or nothing | `tests/proof_of_boundary/test_output_gate_containment.py` |
| PB-9 | Entry-point auth | request with no / wrong bearer token; oversize body or metadata | `401`; `413` | `TestValidationRejectionThroughInvoke` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt() checkpoint | skipped with reason (real assertion when HITL wired) | `test_pb7_hitl_interrupt_propagation.py` |

## 4. Business Logic Tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path report generation | `_VALID_PAYLOAD` | 5-section 温対法/GX report; `EMISSIONS COMPLIANCE REPORT` + facility_id present | `test_backbone_invoke_succeeds_and_returns_output`, `TestInnerDomainGraph` |
| BL-02 | Scope 1 emission-factor computation | coal 1,000 t | `scope1_tco2 = 2,330.0` (1,000 × 2.33 環境省 factor) | `test_scope1_uses_environment_ministry_fuel_factor` |
| BL-03 | Scope 2 grid-electricity factor | 1,000,000 kWh | `scope2_tco2 = 434.0` (× 0.000434 tCO2/kWh) | `test_scope2_uses_grid_electricity_factor` |
| BL-04 | Scope 3 summation | one category, 5,000 tCO2e | `scope3_tco2 = total_tco2 = 5,000.0` | `test_scope3_is_summed` |
| BL-05 | Emitter classification | total 150,000 / 5,000 / 100 tCO2e | `large_emitter_gx_ets` / `specified_emitter` / `below_mandatory_threshold` | `TestParseEmissionsDataNode` |
| BL-06 | Data-quality flag on unknown fuel | `fuel_type="plutonium"` | `unknown_scope1_fuel_type:*` flag emitted | `test_unknown_fuel_type_flagged` |
| BL-07 | 温対法 mandatory-reporting threshold | total = 5,000 tCO2e | `reporting_required=True`, `gx_ets_applicable=False` | `test_reporting_required_over_mandatory_threshold` |
| BL-08 | GX-ETS large-emitter threshold | total = 150,000 tCO2e | `reporting_required=True`, `gx_ets_applicable=True` | `test_gx_ets_applicable_over_large_threshold` |
| BL-09 | Below mandatory threshold | total = 2,999 tCO2e | `reporting_required=False` | `test_not_required_under_mandatory_threshold` |
| BL-10 | 5-section assembly + compliance trailer | 5 rendered sections + compliance flags | all 5 headers + `REGULATORY COMPLIANCE NOTE` + `DISCLAIMER`; 温対法 YES/NO reflects flag | `TestOutputFormatNode` |
| BL-11 | Year-over-year comparison | previous supplied / missing | "Previous Year" line / "No prior-year" placeholder | `test_year_over_year_*` |
| BL-12 | Reduction-commitments placeholder | empty commitments | "No reduction commitments" placeholder | `test_reduction_commitments_placeholder_when_empty` |
| BL-13 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 5 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |

### Negative / boundary cases

The two columns below are not interchangeable. A refusal the caller can correct
**completes** the run (`status=success`) carrying the reason; a refusal the
caller cannot correct **terminates** it (`status=error`). Asserting one where
the other belongs is the defect these cases exist to catch.

| Case | Node | Expected |
|------|------|----------|
| empty `user_input` | PreProcessNode | completes, `status=success`, error_log "empty" |
| invalid JSON | PreProcessNode | completes, `status=success`, error_log "invalid JSON" |
| JSON root not an object | PreProcessNode | completes, `status=success`, error_log "object" |
| missing `scope1_activities` | PreProcessNode | completes, `status=success`, error_log "scope1_activities" |
| empty `facility_id` | InputValidateNode | completes, `status=success`, error_log "facility_id" |
| `reporting_year` out of `[2005-2100]` | InputValidateNode | completes, `status=success`, error_log "reporting_year" |
| no scope 1/2/3 activities | InputValidateNode | completes, `status=success`, error_log "activities" |
| missing `emissions_data` | Parse / Generate / Compliance | terminates, `status=error` — a broken invariant, not a value a caller can restate |
| missing `report_sections` | OutputFormatNode | terminates, `status=error` — a broken invariant |
| empty `compliance_report` | PostProcessNode | fallback message, `status=success` |
| credential leak in any outgoing representation | PostProcessNode | withheld + sanitised stub, `status=error` |
| verbatim caller payload embedded in the report | PostProcessNode | replaced with `[REDACTED]`, `status=success` |
| off-grid figure in report text, sections or flags | PostProcessNode | snapped to the nearest 1,000 + audit event |
| non-finite / negative / over-magnitude activity value | InputValidateNode | completes, `status=success`, error_log names the field; value never echoed |
| free-text or over-length `company_name`, `fuel_type`, `category`, commitment | InputValidateNode | completes, `status=success`, error_log names the field |
| more than 500 activity records or 20 commitments | InputValidateNode | completes, `status=success`, error_log names the field |
| request body or `input_context` over 256 KiB | `src/api/server.py` | HTTP `413` |
| missing / wrong bearer token when auth is configured | `src/api/server.py` | HTTP `401`, generic body |

## 5. Test Execution Summary

- Execution: `python -m pytest tests/ -v`.
- Runner of record: pytest under the real framework wheel (`agenticstar-agentcore` as installed by
  central CI; local verification shadows the same wheel).
- Total: 293 tests — **292 passed, 1 skipped** (PB-7 skip stub, by design).
- Coverage: node + graph modules exercised on both success and error paths,
  the output boundary probed in both directions, and the public HTTP path
  exercised end-to-end against the real compiled agent.
