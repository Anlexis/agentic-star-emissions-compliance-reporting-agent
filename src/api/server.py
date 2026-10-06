"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import EmissionsComplianceReportGeneratorAgent

app = FastAPI(title="Agent")

agent = EmissionsComplianceReportGeneratorAgent()
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="ene-c2-007", agent_name="EmissionsComplianceReportGeneratorAgent"))

# Adapter-level structural caps: the request body's payload string and the
# structured input_context are each capped before anything downstream parses
# them. Oversize requests are rejected at the boundary with no echo.
_MAX_INPUT_BYTES = 262_144  # 256 KiB
_MAX_INPUT_CONTEXT_BYTES = 262_144  # 256 KiB (serialized)


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller request metadata, forwarded to the graph as the
    # invoke() input_context parameter. Optional; absent metadata degrades
    # to an empty dict.
    input_context: dict[str, Any] | None = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # the platform's auth middleware) — a deployment-level caller credential,
    # not an agent secret, so the secrets provider does not apply (no
    # invocation context exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    # Structural size caps (reject at the adapter; no echo of the payload).
    if len(req.input.encode("utf-8", "ignore")) > _MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="Request input exceeds the size limit.")
    input_context = req.input_context or {}
    try:
        context_bytes = len(json.dumps(input_context, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="input_context is not serializable.") from exc
    if context_bytes > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the size limit.")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result: dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=input_context)
        return result


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "EmissionsComplianceReportGeneratorAgent"}
