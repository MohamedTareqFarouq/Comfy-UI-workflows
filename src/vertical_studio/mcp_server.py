from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import threading
import time
import uuid
from functools import wraps
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .config import Settings, load_settings
from .headless import HeadlessMediaService, ProductionService
from .headless_schemas import (
    ApprovalPolicy,
    CancelRequest,
    EvaluationRequest,
    ImageGenerationRequest,
    ProductionStartRequest,
    ProductionTaskRequest,
    RenderRequest,
    VideoGenerationRequest,
)
from .schemas import utc_now


class MCPCallTracer:
    _secret_words = ("password", "passwd", "token", "secret", "api_key", "authorization")
    _summary_keys = (
        "id", "operation_id", "production_session_id", "artifact_id", "task_id",
        "approval_id", "capability_id", "state", "status", "result_artifact_ids",
        "allowed_actions", "failure", "error",
    )

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def _safe(cls, value: Any, key: str = "") -> Any:
        if any(word in key.lower() for word in cls._secret_words):
            return "[REDACTED]"
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        elif hasattr(value, "value") and not isinstance(value, (str, bytes)):
            value = value.value
        if isinstance(value, dict):
            return {str(item_key): cls._safe(item_value, str(item_key)) for item_key, item_value in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @classmethod
    def _summary(cls, result: Any) -> Any:
        safe = cls._safe(result)
        if isinstance(safe, dict):
            summary = {key: safe[key] for key in cls._summary_keys if key in safe}
            if summary:
                return summary
            return {"result_type": "object", "keys": sorted(safe)[:40]}
        if isinstance(safe, list):
            return {"result_type": "list", "count": len(safe)}
        return {"result_type": type(result).__name__}

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        print(f"MCP_TRACE {line}", file=sys.stderr, flush=True)

    def recent(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        if not self.path.exists():
            return {"trace_file": str(self.path), "calls": []}
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        calls = []
        for line in lines[-limit:]:
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                calls.append({"phase": "trace_error", "message": "Invalid JSONL record"})
        return {"trace_file": str(self.path), "calls": calls}


def build_mcp(settings: Settings | None = None) -> tuple[MCPServer, HeadlessMediaService]:
    settings = settings or load_settings()
    media = HeadlessMediaService(settings)
    production = ProductionService(media)
    server = MCPServer(
        "vertical-media-production",
        description="Capability-routed image and video production with durable operations and human control.",
        version="0.2.0",
        instructions=(
            "Use the exact input fields and enum values shown in each tool schema. Discover capabilities "
            "before generation. Use artifact IDs and explicit reference roles. A queued or running operation "
            "is not complete: poll media_get_operation until succeeded, failed, or cancelled. If it fails, "
            "read its failure field and mcp_get_recent_calls; do not claim success or invent missing values. "
            + ("An explicit unqualified-capability override is enabled: you may select unqualified capabilities, but must disclose that they are unqualified. "
               if settings.allow_unqualified_capabilities else "Unqualified capabilities cannot execute unless VVS_ALLOW_UNQUALIFIED_CAPABILITIES is enabled. ")
            + "Generation is never approval. OpenCode owns creative reasoning; this service owns validated "
            "execution, persistence, approvals, budgets, and cancellation."
        ),
    )
    tracer = MCPCallTracer(settings.data_dir / "logs" / "mcp-calls.jsonl")

    def traced_tool(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            call_id = uuid.uuid4().hex
            started = time.perf_counter()
            try:
                bound = inspect.signature(function).bind_partial(*args, **kwargs)
                arguments = tracer._safe(bound.arguments)
            except Exception:
                arguments = {"argument_names": sorted(kwargs)}
            tracer.write({
                "timestamp": utc_now(), "phase": "started", "trace_id": call_id,
                "tool": function.__name__, "arguments": arguments,
            })
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                message = f"{function.__name__} failed [trace_id={call_id}]: {type(exc).__name__}: {exc}. Use mcp_get_recent_calls to inspect this call."
                tracer.write({
                    "timestamp": utc_now(), "phase": "failed", "trace_id": call_id,
                    "tool": function.__name__, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error_type": type(exc).__name__, "error": str(exc),
                })
                raise ToolError(message) from exc
            tracer.write({
                "timestamp": utc_now(), "phase": "completed", "trace_id": call_id,
                "tool": function.__name__, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "result": tracer._summary(result),
            })
            return result

        return server.tool()(invoke)

    @server.tool()
    def mcp_get_recent_calls(limit: Annotated[int, Field(ge=1, le=100, description="How many newest trace records to return, from 1 through 100. Default 20.")] = 20) -> dict[str, Any]:
        """Read recent MCP call trace records, including tool name, arguments, result IDs, duration, and errors.

        Args:
            limit: Number of most recent trace records to return (1 through 100; default 20).
        """
        return tracer.recent(limit)

    @traced_tool
    def mcp_get_status() -> dict[str, Any]:
        """Report this MCP process's execution mode, backend, ComfyUI reachability, and capability qualification.

        Call before any image or video generation. Simulation uses a local mock and never contacts ComfyUI.
        """
        simulation = settings.is_simulation
        comfy = (
            {"available": False, "checked": False, "reason": "Simulation uses a local mock backend."}
            if simulation else {**media.generator.health(), "checked": True}
        )
        qwen = (
            {"available": False, "checked": False, "reason": "Simulation does not use Qwen."}
            if simulation else {**media.model.health(), "checked": True}
        )
        capabilities = [
            {
                "id": item.id,
                "media_type": item.media_type.value,
                "qualification_state": item.qualification_state.value,
            }
            for item in media.registry.list()
        ]
        real_ready = not simulation and bool(comfy.get("available"))
        return {
            "execution_mode": settings.execution_mode,
            "backend": "mock_simulator" if simulation else "comfyui",
            "comfyui": comfy,
            "qwen": qwen,
            "managed_qwen": settings.manage_qwen,
            "qwen_loaded": media.model.owned,
            "allow_unqualified_capabilities": settings.allow_unqualified_capabilities,
            "keyframe_image_backend": settings.keyframe_image_backend,
            "capabilities": capabilities,
            "real_generation_ready_capabilities": [
                item["id"] for item in capabilities
                if real_ready and item["qualification_state"] == "qualified"
                and item["media_type"] in {"image", "video"}
            ],
            "unqualified_override_capabilities": [
                item["id"] for item in capabilities
                if settings.allow_unqualified_capabilities
                and item["qualification_state"] == "unqualified"
                and item["media_type"] in {"image", "video", "evaluation"}
            ],
            "simulation_warning": (
                "Generated files are local simulation placeholders, not ComfyUI results."
                if simulation else None
            ),
        }

    @traced_tool
    def media_list_capabilities() -> list[dict[str, Any]]:
        """List every capability, including disabled, missing, and unqualified entries."""
        return [item.model_dump(mode="json") for item in media.registry.list()]

    @traced_tool
    def media_inspect_capability(capability_id: Annotated[str, Field(description="Exact stable capability ID returned by media_list_capabilities.")]) -> dict[str, Any]:
        """Return the complete contract for one stable capability ID."""
        return media.registry.get(capability_id).model_dump(mode="json")

    @traced_tool
    def media_import_reference(source_path: Annotated[str, Field(description="Readable path to an image or video file on the MCP server machine.")],
                               role: Annotated[Literal["identity", "environment", "prop", "style", "composition", "pose", "start_frame", "end_frame", "motion_continuity"], Field(description="Reference purpose: identity, environment, prop, style, composition, pose, start_frame, end_frame, or motion_continuity.")],
                               idempotency_key: Annotated[str, Field(description="Unique key for this import; reuse only for the same file and role.")],
                               production_session_id: Annotated[str | None, Field(description="Optional ID returned by production_start. Omit for a standalone import.")] = None) -> dict[str, Any]:
        """Copy a local image or video into managed storage and return its artifact record."""
        return media.import_reference(source_path, role, idempotency_key, production_session_id)

    @traced_tool
    def media_analyze_reference(artifact_ids: Annotated[list[str], Field(description="One or more existing artifact IDs returned by media_import_reference.")],
                                idempotency_key: Annotated[str, Field(description="Unique key for this analysis request.")],
                                production_session_id: Annotated[str | None, Field(description="Optional ID returned by production_start.")] = None) -> dict[str, Any]:
        """Queue typed reference analysis without starting generation."""
        return media.analyze(artifact_ids, idempotency_key, production_session_id)

    @traced_tool
    def media_generate_image(request: Annotated[ImageGenerationRequest, Field(description="Image prompt, dimensions, references, seed, candidate count, and idempotency key.")]) -> dict[str, Any]:
        """Queue routed T2I, Klein reference editing, or optional Qwen Image 2.1 editing via preferred_capability."""
        return {**media.generate_image(request), "execution_mode": settings.execution_mode,
            "backend": "mock_simulator" if settings.is_simulation else "comfyui"}

    @traced_tool
    def media_generate_video(request: Annotated[VideoGenerationRequest, Field(description="Video prompt, start/end frame references, motion, dimensions, frame count, seed, and idempotency key.")]) -> dict[str, Any]:
        """Queue Wan I2V or FLF2V; roles determine the mode and are never discarded."""
        return {**media.generate_video(request), "execution_mode": settings.execution_mode,
            "backend": "mock_simulator" if settings.is_simulation else "comfyui"}

    @traced_tool
    def media_evaluate(request: Annotated[EvaluationRequest, Field(description="Artifact IDs and optional requirements. Set use_visual_critique=true to request Qwen review when qualified or override-eligible; otherwise performs technical-only QA.")]) -> dict[str, Any]:
        """Queue technical-only QA or Qwen-backed visual critique; never automatically retries."""
        return media.evaluate(request)

    @traced_tool
    def media_render_sequence(request: Annotated[RenderRequest, Field(description="Approved video artifact IDs in exact playback order and an idempotency key.")]) -> dict[str, Any]:
        """Render explicitly ordered approved video artifacts without autonomous shot selection."""
        return media.render(request)

    @traced_tool
    def media_get_operation(operation_id: Annotated[str, Field(description="Operation ID returned by a queueing tool. Poll until state is succeeded, failed, or cancelled.")]) -> dict[str, Any]:
        """Inspect one asynchronous operation."""
        return {**media.store.operation(operation_id), "execution_mode": settings.execution_mode,
            "backend": "mock_simulator" if settings.is_simulation else "comfyui"}

    @traced_tool
    def media_list_operations(production_session_id: Annotated[str | None, Field(description="Optional production session ID to filter by.")] = None,
                              state: Annotated[Literal["queued", "running", "succeeded", "failed", "cancelled"] | None, Field(description="Optional exact operation state filter: queued, running, succeeded, failed, or cancelled.")] = None) -> list[dict[str, Any]]:
        """List operations, optionally filtered by production session and state."""
        return media.store.list_operations(production_session_id, state)

    @traced_tool
    def media_retry_operation(operation_id: Annotated[str, Field(description="ID of a failed or cancelled operation.")],
                              idempotency_key: Annotated[str, Field(description="New unique key authorizing this retry.")]) -> dict[str, Any]:
        """Retry a failed or deliberately cancelled operation with explicit authorization."""
        return media.retry(operation_id, idempotency_key)

    @traced_tool
    def media_cancel_operation(operation_id: Annotated[str, Field(description="ID of the queued or running operation to cancel.")],
                               reason: Annotated[str, Field(description="Why this operation is being cancelled.")],
                               actor: Annotated[str, Field(description="Who requested cancellation; defaults to user.")] = "user") -> dict[str, Any]:
        """Cancel one queued or running operation and scope any ComfyUI interrupt to its prompt."""
        return media.cancel_operation(operation_id, reason, actor)

    @traced_tool
    def media_get_artifact(artifact_id: Annotated[str, Field(description="Artifact ID returned by a generation or import tool.")]) -> dict[str, Any]:
        """Inspect artifact lineage, disposition, QA, hashes, and generation metadata."""
        return media.store.artifact(artifact_id)

    @traced_tool
    def media_set_artifact_disposition(artifact_id: Annotated[str, Field(description="Artifact ID to update.")],
                                       disposition: Annotated[Literal["canonical", "generated", "approved", "rejected", "superseded"], Field(description="Set to canonical, generated, approved, rejected, or superseded. Only approved means accepted for rendering.")],
                                       notes: Annotated[str, Field(description="Optional reason or review note.")] = "") -> dict[str, Any]:
        """Explicitly approve, reject, supersede, or promote an artifact; promotion is never automatic."""
        with media.store.connect() as connection:
            changed = connection.execute(
                "UPDATE media_artifacts SET disposition=?, user_notes=? WHERE id=?",
                (disposition, notes, artifact_id),
            ).rowcount
            if not changed: raise KeyError("artifact not found")
        return media.store.artifact(artifact_id)

    @traced_tool
    def production_start(request: Annotated[ProductionStartRequest, Field(description="User goal, prompt, references, output requirements, approval policy, budgets, and unique idempotency key.")]) -> dict[str, Any]:
        """Create a durable dynamic production task graph for OpenCode to orchestrate."""
        return production.start(request)

    @traced_tool
    def production_inspect(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")]) -> dict[str, Any]:
        """Inspect session interpretation, tasks, handoffs, operations, approvals, budgets, and events."""
        return production.inspect(production_session_id)

    @traced_tool
    def production_get_situation(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")]) -> dict[str, Any]:
        """Return compact state, budgets, current work, steering, and allowed next actions."""
        return production.situation(production_session_id)

    @traced_tool
    def production_create_task(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                               request: Annotated[ProductionTaskRequest, Field(description="Stage, objective, dependency task IDs, input artifacts, acceptance criteria, and unique idempotency key.")]) -> dict[str, Any]:
        """Create an agent-selected stage with explicit dependencies; stages may be skipped or revisited."""
        return production.create_task(production_session_id, request)

    @traced_tool
    def production_invalidate_task(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                                   task_id: Annotated[str, Field(description="Task ID from production_inspect.")],
                                   reason: Annotated[str, Field(description="Why this task and its dependent tasks are no longer valid.")]) -> dict[str, Any]:
        """Invalidate one task and its transitive dependents while preserving unrelated artifacts."""
        return production.invalidate_task(production_session_id, task_id, reason)

    @traced_tool
    def production_update_task(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                               task_id: Annotated[str, Field(description="Task ID from production_inspect.")],
                               status: Annotated[Literal["pending", "ready", "running", "completed", "rejected", "invalidated", "skipped"], Field(description="Task state: pending, ready, running, completed, rejected, invalidated, or skipped.")],
                               handoff: Annotated[dict[str, Any], Field(description="Structured role output and concise evidence for the next task.")],
                               artifact_ids: Annotated[list[str] | None, Field(description="Artifact IDs produced by this task, if any.")] = None,
                               invalidated_reason: Annotated[str | None, Field(description="Required explanation when marking a task invalidated.")] = None) -> dict[str, Any]:
        """Persist an OpenCode role result and unlock only tasks whose dependencies are complete."""
        return production.update_task(production_session_id, task_id, status, handoff,
                                      artifact_ids or [], invalidated_reason)

    @traced_tool
    def production_request_approval(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                                    operation_spec: Annotated[dict[str, Any], Field(description="Exact proposed operation object containing capability_id and the complete request payload. The later generation call must match it exactly.")],
                                    reason: Annotated[str, Field(description="Why this operation needs approval.")],
                                    dependencies: Annotated[list[str], Field(description="Task or artifact IDs that this operation depends on.")],
                                    affected_shots: Annotated[list[str], Field(description="Shot identifiers affected by this operation.")],
                                    attempt: Annotated[int, Field(ge=1, description="Attempt number for this operation, starting at 1.")],
                                    remaining_budget: Annotated[dict[str, Any], Field(description="Remaining operation, time, candidate, and storage budget values.")]) -> dict[str, Any]:
        """Bind approval to {capability_id, request}; request must exactly match the later media tool payload."""
        canonical = json.dumps(operation_spec, sort_keys=True, separators=(",", ":"))
        approval_id = uuid.uuid4().hex
        record = {**operation_spec, "reason": reason, "dependencies": dependencies,
                  "affected_shots": affected_shots, "attempt": attempt, "remaining_budget": remaining_budget}
        with media.store.connect() as connection:
            if connection.execute("SELECT 1 FROM production_sessions WHERE id=?", (production_session_id,)).fetchone() is None:
                raise KeyError(f"production session not found: {production_session_id}")
            connection.execute(
                "INSERT INTO production_approvals VALUES(?,?,?,?,?,?,?,?)",
                (approval_id, production_session_id, hashlib.sha256(canonical.encode()).hexdigest(),
                 json.dumps(record), "pending", "", utc_now(), None),
            )
            production._event(connection, production_session_id, "approval_requested", {"approval_id": approval_id})
        return {**production.inspect(production_session_id), "created_approval_id": approval_id,
            "created_approval_status": "pending"}

    @traced_tool
    def production_list_approvals(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")]) -> list[dict[str, Any]]:
        """List pending and decided approvals for one session."""
        return production.list_approvals(production_session_id)

    @traced_tool
    def production_approve(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                           approval_id: Annotated[str, Field(description="Pending approval ID from production_list_approvals.")],
                           reason: Annotated[str, Field(description="Reason for approving this exact operation.")] = "approved") -> dict[str, Any]:
        """Approve only the exact operation specification stored in an approval request."""
        return production.decide_approval(production_session_id, approval_id, True, reason)

    @traced_tool
    def production_reject(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                          approval_id: Annotated[str, Field(description="Pending approval ID from production_list_approvals.")],
                          reason: Annotated[str, Field(description="Why this proposed operation is rejected.")]) -> dict[str, Any]:
        """Reject a proposal and return the user's reason to the production task graph."""
        return production.decide_approval(production_session_id, approval_id, False, reason)

    @traced_tool
    def production_resolve_approval(approval_id: Annotated[str, Field(description="Exact approval ID returned by production_request_approval or production_list_approvals.")],
                                   decision: Annotated[Literal["approved", "rejected"], Field(description="Final decision: approved or rejected.")],
                                   reason: Annotated[str, Field(description="Reason for the user's decision.")]) -> dict[str, Any]:
        """Approve or reject one pending approval by ID; resolves its session automatically and safely repeats the same decision."""
        return production.decide_approval_by_id(approval_id, decision == "approved", reason)

    @traced_tool
    def production_set_approval_policy(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                                      policy: Annotated[ApprovalPolicy, Field(description="Approval preset and optional custom rules.")]) -> dict[str, Any]:
        """Change the approval preset or custom rules for subsequent operations."""
        return production.set_policy(production_session_id, policy)

    @traced_tool
    def production_cancel(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                          request: Annotated[CancelRequest, Field(description="Which active and queued operations to cancel, the reason, and whether to continue or pause afterward.")]) -> dict[str, Any]:
        """Selectively cancel active or queued image/video work and choose controller disposition."""
        return production.cancel(production_session_id, request)

    @traced_tool
    def production_pause(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                         generation_only: Annotated[bool, Field(description="True pauses new generation only; false pauses the full production session.")] = False) -> dict[str, Any]:
        """Pause generation only or the entire production session."""
        return production.pause(production_session_id, generation_only)

    @traced_tool
    def production_resume(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")]) -> dict[str, Any]:
        """Explicitly resume a paused production session and retained queues."""
        return production.resume(production_session_id)

    @traced_tool
    def production_retry_cancelled(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                                   operation_id: Annotated[str, Field(description="Cancelled operation ID belonging to this session.")],
                                   idempotency_key: Annotated[str, Field(description="New unique key authorizing recreation of this cancelled operation.")]) -> dict[str, Any]:
        """Explicitly authorize recreation of a deliberately cancelled operation."""
        operation = media.store.operation(operation_id)
        if operation["production_session_id"] != production_session_id:
            raise ValueError("operation belongs to another production session")
        return media.retry(operation_id, idempotency_key)

    @traced_tool
    def production_emergency_stop(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                                  reason: Annotated[str, Field(description="Why all active and queued work must stop.")]) -> dict[str, Any]:
        """Stop active work, clear queued generation, pause every task, and preserve artifacts and logs."""
        return production.emergency_stop(production_session_id, reason)

    @traced_tool
    def production_steer(production_session_id: Annotated[str, Field(description="Production session ID returned by production_start.")],
                         message: Annotated[str, Field(description="New user direction to record for the controller.")],
                         actor: Annotated[str, Field(description="Who supplied the direction; defaults to user.")] = "user") -> dict[str, Any]:
        """Append user steering without forcing a global state transition."""
        with media.store.connect() as connection:
            row = connection.execute("SELECT steering_json FROM production_sessions WHERE id=?", (production_session_id,)).fetchone()
            if row is None: raise KeyError("production session not found")
            steering = json.loads(row[0]); steering.append({"message": message, "actor": actor, "at": utc_now()})
            connection.execute("UPDATE production_sessions SET steering_json=?, updated_at=? WHERE id=?",
                               (json.dumps(steering), utc_now(), production_session_id))
            production._event(connection, production_session_id, "steering_added", steering[-1])
        return production.inspect(production_session_id)

    original_call_tool = server.call_tool

    async def trace_dispatch_errors(name, arguments, context=None):
        try:
            return await original_call_tool(name, arguments, context)
        except Exception as exc:
            if "trace_id=" in str(exc):
                raise
            trace_id = uuid.uuid4().hex
            started = time.perf_counter()
            tracer.write({
                "timestamp": utc_now(), "phase": "started", "trace_id": trace_id,
                "tool": name, "arguments": tracer._safe(arguments),
            })
            detail = exc
            while detail.__cause__ is not None:
                detail = detail.__cause__
            message = f"MCP tool call {name!r} failed [trace_id={trace_id}]: {type(detail).__name__}: {detail}. Check the tool schema and mcp_get_recent_calls."
            tracer.write({
                "timestamp": utc_now(), "phase": "failed", "trace_id": trace_id,
                "tool": name, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error_type": type(detail).__name__, "error": str(detail),
            })
            raise ToolError(message) from exc

    server.call_tool = trace_dispatch_errors
    return server, media


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Vertical Media Production MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default=os.getenv("VVS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VVS_MCP_PORT", "8766")))
    parser.add_argument(
        "--stateless-http", action="store_true",
        help="Disable MCP HTTP transport sessions (production sessions remain persisted)",
    )
    args = parser.parse_args()
    server, media = build_mcp()
    media.start()
    try:
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(transport="streamable-http", host=args.host, port=args.port,
                       stateless_http=args.stateless_http, json_response=True)
    finally:
        media.stop()


if __name__ == "__main__":
    main()
