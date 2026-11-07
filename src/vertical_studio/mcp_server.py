from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Literal

from mcp.server import MCPServer

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


def build_mcp(settings: Settings | None = None) -> tuple[MCPServer, HeadlessMediaService]:
    settings = settings or load_settings()
    media = HeadlessMediaService(settings)
    production = ProductionService(media)
    server = MCPServer(
        "vertical-media-production",
        description="Capability-routed image and video production with durable operations and human control.",
        version="0.2.0",
        instructions=(
            "Discover capabilities before generation. Use artifact IDs and explicit reference roles. "
            "Poll operations instead of holding tool calls open. OpenCode owns creative role reasoning; "
            "the service owns validated execution, persistence, approvals, budgets, and cancellation."
        ),
    )

    @server.tool()
    def media_list_capabilities() -> list[dict[str, Any]]:
        """List every capability, including disabled, missing, and unqualified entries."""
        return [item.model_dump(mode="json") for item in media.registry.list()]

    @server.tool()
    def media_inspect_capability(capability_id: str) -> dict[str, Any]:
        """Return the complete contract for one stable capability ID."""
        return media.registry.get(capability_id).model_dump(mode="json")

    @server.tool()
    def media_import_reference(source_path: str, role: str, idempotency_key: str,
                               production_session_id: str | None = None) -> dict[str, Any]:
        """Copy a local image or video into managed storage and return its artifact record."""
        return media.import_reference(source_path, role, idempotency_key, production_session_id)

    @server.tool()
    def media_analyze_reference(artifact_ids: list[str], idempotency_key: str,
                                production_session_id: str | None = None) -> dict[str, Any]:
        """Queue typed reference analysis without starting generation."""
        return media.analyze(artifact_ids, idempotency_key, production_session_id)

    @server.tool()
    def media_generate_image(request: ImageGenerationRequest) -> dict[str, Any]:
        """Queue routed T2I, single-reference, or one-through-four-reference image generation."""
        return media.generate_image(request)

    @server.tool()
    def media_generate_video(request: VideoGenerationRequest) -> dict[str, Any]:
        """Queue Wan I2V or FLF2V; roles determine the mode and are never discarded."""
        return media.generate_video(request)

    @server.tool()
    def media_evaluate(request: EvaluationRequest) -> dict[str, Any]:
        """Queue technical validation and structured QA without automatically retrying."""
        return media.evaluate(request)

    @server.tool()
    def media_render_sequence(request: RenderRequest) -> dict[str, Any]:
        """Render explicitly ordered approved video artifacts without autonomous shot selection."""
        return media.render(request)

    @server.tool()
    def media_get_operation(operation_id: str) -> dict[str, Any]:
        """Inspect one asynchronous operation."""
        return media.store.operation(operation_id)

    @server.tool()
    def media_list_operations(production_session_id: str | None = None,
                              state: str | None = None) -> list[dict[str, Any]]:
        """List operations, optionally filtered by production session and state."""
        return media.store.list_operations(production_session_id, state)

    @server.tool()
    def media_retry_operation(operation_id: str, idempotency_key: str) -> dict[str, Any]:
        """Retry a failed or deliberately cancelled operation with explicit authorization."""
        return media.retry(operation_id, idempotency_key)

    @server.tool()
    def media_cancel_operation(operation_id: str, reason: str, actor: str = "user") -> dict[str, Any]:
        """Cancel one queued or running operation and scope any ComfyUI interrupt to its prompt."""
        return media.cancel_operation(operation_id, reason, actor)

    @server.tool()
    def media_get_artifact(artifact_id: str) -> dict[str, Any]:
        """Inspect artifact lineage, disposition, QA, hashes, and generation metadata."""
        return media.store.artifact(artifact_id)

    @server.tool()
    def media_set_artifact_disposition(artifact_id: str,
                                       disposition: Literal["canonical", "generated", "approved", "rejected", "superseded"],
                                       notes: str = "") -> dict[str, Any]:
        """Explicitly approve, reject, supersede, or promote an artifact; promotion is never automatic."""
        with media.store.connect() as connection:
            changed = connection.execute(
                "UPDATE media_artifacts SET disposition=?, user_notes=? WHERE id=?",
                (disposition, notes, artifact_id),
            ).rowcount
            if not changed: raise KeyError("artifact not found")
        return media.store.artifact(artifact_id)

    @server.tool()
    def production_start(request: ProductionStartRequest) -> dict[str, Any]:
        """Create a durable dynamic production task graph for OpenCode to orchestrate."""
        return production.start(request)

    @server.tool()
    def production_inspect(production_session_id: str) -> dict[str, Any]:
        """Inspect session interpretation, tasks, handoffs, operations, approvals, budgets, and events."""
        return production.inspect(production_session_id)

    @server.tool()
    def production_get_situation(production_session_id: str) -> dict[str, Any]:
        """Return compact state, budgets, current work, steering, and allowed next actions."""
        return production.situation(production_session_id)

    @server.tool()
    def production_create_task(production_session_id: str, request: ProductionTaskRequest) -> dict[str, Any]:
        """Create an agent-selected stage with explicit dependencies; stages may be skipped or revisited."""
        return production.create_task(production_session_id, request)

    @server.tool()
    def production_invalidate_task(production_session_id: str, task_id: str, reason: str) -> dict[str, Any]:
        """Invalidate one task and its transitive dependents while preserving unrelated artifacts."""
        return production.invalidate_task(production_session_id, task_id, reason)

    @server.tool()
    def production_update_task(production_session_id: str, task_id: str, status: str,
                               handoff: dict[str, Any], artifact_ids: list[str] | None = None,
                               invalidated_reason: str | None = None) -> dict[str, Any]:
        """Persist an OpenCode role result and unlock only tasks whose dependencies are complete."""
        return production.update_task(production_session_id, task_id, status, handoff,
                                      artifact_ids or [], invalidated_reason)

    @server.tool()
    def production_request_approval(production_session_id: str, operation_spec: dict[str, Any],
                                    reason: str, dependencies: list[str], affected_shots: list[str],
                                    attempt: int, remaining_budget: dict[str, Any]) -> dict[str, Any]:
        """Bind approval to {capability_id, request}; request must exactly match the later media tool payload."""
        canonical = json.dumps(operation_spec, sort_keys=True, separators=(",", ":"))
        approval_id = uuid.uuid4().hex
        record = {**operation_spec, "reason": reason, "dependencies": dependencies,
                  "affected_shots": affected_shots, "attempt": attempt, "remaining_budget": remaining_budget}
        with media.store.connect() as connection:
            connection.execute(
                "INSERT INTO production_approvals VALUES(?,?,?,?,?,?,?,?)",
                (approval_id, production_session_id, hashlib.sha256(canonical.encode()).hexdigest(),
                 json.dumps(record), "pending", "", utc_now(), None),
            )
            production._event(connection, production_session_id, "approval_requested", {"approval_id": approval_id})
        return production.inspect(production_session_id)

    @server.tool()
    def production_list_approvals(production_session_id: str) -> list[dict[str, Any]]:
        """List pending and decided approvals for one session."""
        return production.list_approvals(production_session_id)

    @server.tool()
    def production_approve(production_session_id: str, approval_id: str, reason: str = "approved") -> dict[str, Any]:
        """Approve only the exact operation specification stored in an approval request."""
        return production.decide_approval(production_session_id, approval_id, True, reason)

    @server.tool()
    def production_reject(production_session_id: str, approval_id: str, reason: str) -> dict[str, Any]:
        """Reject a proposal and return the user's reason to the production task graph."""
        return production.decide_approval(production_session_id, approval_id, False, reason)

    @server.tool()
    def production_set_approval_policy(production_session_id: str, policy: ApprovalPolicy) -> dict[str, Any]:
        """Change the approval preset or custom rules for subsequent operations."""
        return production.set_policy(production_session_id, policy)

    @server.tool()
    def production_cancel(production_session_id: str, request: CancelRequest) -> dict[str, Any]:
        """Selectively cancel active or queued image/video work and choose controller disposition."""
        return production.cancel(production_session_id, request)

    @server.tool()
    def production_pause(production_session_id: str, generation_only: bool = False) -> dict[str, Any]:
        """Pause generation only or the entire production session."""
        return production.pause(production_session_id, generation_only)

    @server.tool()
    def production_resume(production_session_id: str) -> dict[str, Any]:
        """Explicitly resume a paused production session and retained queues."""
        return production.resume(production_session_id)

    @server.tool()
    def production_retry_cancelled(production_session_id: str, operation_id: str,
                                   idempotency_key: str) -> dict[str, Any]:
        """Explicitly authorize recreation of a deliberately cancelled operation."""
        operation = media.store.operation(operation_id)
        if operation["production_session_id"] != production_session_id:
            raise ValueError("operation belongs to another production session")
        return media.retry(operation_id, idempotency_key)

    @server.tool()
    def production_emergency_stop(production_session_id: str, reason: str) -> dict[str, Any]:
        """Stop active work, clear queued generation, pause every task, and preserve artifacts and logs."""
        return production.emergency_stop(production_session_id, reason)

    @server.tool()
    def production_steer(production_session_id: str, message: str, actor: str = "user") -> dict[str, Any]:
        """Append user steering without forcing a global state transition."""
        with media.store.connect() as connection:
            row = connection.execute("SELECT steering_json FROM production_sessions WHERE id=?", (production_session_id,)).fetchone()
            if row is None: raise KeyError("production session not found")
            steering = json.loads(row[0]); steering.append({"message": message, "actor": actor, "at": utc_now()})
            connection.execute("UPDATE production_sessions SET steering_json=?, updated_at=? WHERE id=?",
                               (json.dumps(steering), utc_now(), production_session_id))
            production._event(connection, production_session_id, "steering_added", steering[-1])
        return production.inspect(production_session_id)

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
