from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vertical_studio.schemas import (
    ApprovalRequest,
    ArtifactKind,
    ArtifactSelection,
    CanonicalAssetPromptRevisionRequest,
    CanonicalAssetRetryRequest,
    JobKind,
    ProjectCreate,
    ProjectState,
    RenderRequest,
    RetryPromptRevisionRequest,
    RetryRequest,
    SceneState,
)
from vertical_studio.services.media import probe_media


def approve_keyframes(components, project_id: str) -> None:
    for scene in components.db.list_scenes(project_id):
        artifacts = components.db.list_artifacts(project_id, scene.id)
        start = next(
            item
            for item in artifacts
            if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "start"
        )
        end = next(
            item
            for item in artifacts
            if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "end"
        )
        components.orchestrator.approve_keyframes(
            scene.id,
            ArtifactSelection(start_artifact_id=start.id, end_artifact_id=end.id),
            ApprovalRequest(approved=True),
        )


def approve_storyboard_and_references(components, project_id: str):
    reference_jobs = components.orchestrator.approve_storyboard(
        project_id, ApprovalRequest(approved=True)
    )
    assert reference_jobs
    assert all(job.kind == JobKind.CANONICAL_ASSET for job in reference_jobs)
    components.worker.run_until_idle(max_jobs=20)
    assets = components.db.list_canonical_assets(project_id)
    assert assets and all(asset.artifact_id for asset in assets)
    keyframe_jobs = components.orchestrator.approve_canonical_assets(
        project_id, ApprovalRequest(approved=True)
    )
    assert keyframe_jobs
    return keyframe_jobs


def test_keyframe_queue_uses_configured_qwen_backend(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(
            title="Qwen keyframe backend",
            idea="A station keeper greets a luminous train",
            duration_seconds=10,
        )
    )
    components.orchestrator.plan_project(project.id)
    reference_jobs = components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
    components.worker.run_until_idle(max_jobs=20)
    components.orchestrator.settings = replace(
        components.orchestrator.settings, keyframe_image_backend="qwen_image21"
    )

    jobs = components.orchestrator.approve_canonical_assets(
        project.id, ApprovalRequest(approved=True)
    )

    assert reference_jobs
    assert jobs
    assert all(job.payload["image_edit_backend"] == "qwen_image21" for job in jobs)
    components.worker.run_until_idle(max_jobs=20)
    scene = components.db.list_scenes(project.id)[0]
    artifacts = components.db.list_artifacts(project.id, scene.id)
    methods = {artifact.metadata.get("generation_method") for artifact in artifacts}
    assert "qwen_image21_edit_reference_anchor" in methods
    assert "qwen_image21_edit_reference_transition" in methods


@pytest.mark.parametrize("duration", [10, 20, 30])
def test_simulated_pipeline_completes_vertical_projects(components, duration: int) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(
            title=f"{duration}s test",
            idea="A small station keeper guides a luminous train through the fog",
            duration_seconds=duration,
        )
    )
    storyboard = components.orchestrator.plan_project(project.id)
    assert 3 <= len(storyboard.scenes) <= 10
    assert sum(scene.duration_seconds for scene in storyboard.scenes) == pytest.approx(duration)

    first = approve_storyboard_and_references(components, project.id)
    duplicate = components.orchestrator.queue_keyframes(
        components.db.list_scenes(project.id)[0].id
    )
    assert duplicate.id == first[0].id

    components.worker.run_until_idle(max_jobs=20)
    assert components.db.get_project(project.id).state == ProjectState.CONTACT_SHEET_REVIEW
    approve_keyframes(components, project.id)

    components.worker.run_until_idle(max_jobs=20)
    scenes = components.db.list_scenes(project.id)
    assert all(scene.state == SceneState.VIDEO_READY for scene in scenes)

    for scene in scenes:
        components.orchestrator.approve_scene(
            scene.id, ApprovalRequest(approved=True)
        )
    assert components.db.get_project(project.id).state == ProjectState.FINAL_REVIEW

    components.orchestrator.queue_render(project.id, RenderRequest())
    components.worker.run_until_idle()
    assert components.db.get_project(project.id).state == ProjectState.COMPLETE

    rendered = next(
        item
        for item in components.db.list_artifacts(project.id)
        if item.kind == ArtifactKind.RENDER
    )
    assert components.db.list_attempts(project.id)
    manifests = components.db.list_render_manifests(project.id)
    assert manifests[-1]["artifact_id"] == rendered.id
    assert components.db.list_approvals(project.id)
    metadata = probe_media(Path(rendered.path))
    stream = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == (270, 480)
    assert stream["codec_name"] == "h264"
    assert stream["pix_fmt"] == "yuv420p"
    assert not any(s["codec_type"] == "audio" for s in metadata["streams"])

    repeated = components.orchestrator.queue_render(project.id, RenderRequest())
    assert repeated.state.value == "succeeded"
    assert components.db.get_project(project.id).state == ProjectState.COMPLETE


def test_restart_reconciles_stale_project_milestones(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A connected scene survives a server restart", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.db.cancel_project_jobs(project.id)
    for scene in components.db.list_scenes(project.id):
        components.db.transition_scene(scene.id, SceneState.KEYFRAMES_READY)
        components.db.transition_scene(scene.id, SceneState.KEYFRAMES_APPROVED)
        components.db.transition_scene(scene.id, SceneState.VIDEO_QUEUED)
        components.db.transition_scene(scene.id, SceneState.VIDEO_READY)
        components.db.transition_scene(scene.id, SceneState.APPROVED)

    assert components.db.get_project(project.id).state == ProjectState.REFERENCES
    restarted = type(components)(components.settings)
    restarted.start()
    try:
        assert restarted.db.get_project(project.id).state == ProjectState.FINAL_REVIEW
    finally:
        restarted.stop()


def test_exact_replica_goal_persists_and_failed_qa_requires_reasoned_override(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(
            idea="Recreate this source faithfully",
            duration_seconds=10,
            goal_mode="exact_replica",
        )
    )
    assert components.db.get_project(project.id).goal_mode == "exact_replica"
    components.orchestrator.plan_project(project.id)
    scene = components.db.list_scenes(project.id)[0]
    with components.db.connect() as connection:
        connection.execute("UPDATE scenes SET state='qa_failed' WHERE id=?", (scene.id,))

    with pytest.raises(ValueError, match="override_reason"):
        components.orchestrator.approve_scene(
            scene.id, ApprovalRequest(approved=True)
        )

    approved = components.orchestrator.approve_scene(
        scene.id,
        ApprovalRequest(
            approved=True,
            override_reason="Human inspected the frame and accepts the known style deviation.",
        ),
    )
    assert approved.state == SceneState.APPROVED
    approval = components.db.list_approvals(project.id)[-1]
    assert "QA override:" in approval["notes"]


def test_canonical_reference_gate_survives_restart(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="An elderly diner is served by the same waiter", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    reference_jobs = components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
    assert all(job.kind == JobKind.CANONICAL_ASSET for job in reference_jobs)
    with pytest.raises(ValueError, match="must be generated"):
        components.orchestrator.approve_canonical_assets(
            project.id, ApprovalRequest(approved=True)
        )

    components.worker.run_until_idle(max_jobs=20)
    before = components.db.list_canonical_assets(project.id)
    assert all(asset.artifact_id and not asset.approved for asset in before)
    assert components.db.get_project(project.id).state == ProjectState.REFERENCES

    restarted = type(components)(components.settings)
    restarted.start()
    try:
        after = restarted.db.list_canonical_assets(project.id)
        assert [(asset.key, asset.artifact_id) for asset in after] == [
            (asset.key, asset.artifact_id) for asset in before
        ]
        assert restarted.db.get_project(project.id).state == ProjectState.REFERENCES
        keyframe_jobs = restarted.orchestrator.approve_canonical_assets(
            project.id, ApprovalRequest(approved=True)
        )
        assert keyframe_jobs
        assert all(job.kind == JobKind.KEYFRAME for job in keyframe_jobs)
        assert all(asset.approved for asset in restarted.db.list_canonical_assets(project.id))
    finally:
        restarted.stop()


def test_scene_video_retry_is_isolated(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Three connected portrait scenes", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    approve_keyframes(components, project.id)
    components.worker.run_until_idle()

    scenes = components.db.list_scenes(project.id)
    untouched = scenes[1]
    untouched_video = untouched.selected_video_artifact_id
    before = len(components.db.list_jobs(project.id))

    components.orchestrator.queue_video(scenes[0].id, RetryRequest(new_seed=True))
    queued = components.db.list_jobs(project.id)
    assert len(queued) == before + 1
    assert queued[-1].scene_id == scenes[0].id
    assert queued[-1].kind == JobKind.VIDEO

    components.worker.run_until_idle()
    current = components.db.list_scenes(project.id)
    assert current[0].selected_video_artifact_id != scenes[0].selected_video_artifact_id
    assert current[1].selected_video_artifact_id == untouched_video


def test_paused_project_jobs_are_not_claimed_and_expired_lease_recovers(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A paused project", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
    components.orchestrator.pause_project(project.id)
    assert components.db.claim_next_job("worker") is None

    components.orchestrator.resume_project(project.id)
    claimed = components.db.claim_next_job("dead-worker", lease_seconds=-1)
    assert claimed is not None
    recovered = components.db.claim_next_job("new-worker")
    assert recovered is not None
    assert recovered.id == claimed.id
    assert recovered.attempts == 2


def test_events_are_public_json_without_storage_column(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Event serialization", duration_seconds=10)
    )
    event = components.db.events_since(project.id)[0]
    assert "data" in event
    assert "data_json" not in event


def test_cancel_preserves_completed_work_and_stops_queued_jobs(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Cancel safely", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
    cancelled = components.orchestrator.cancel_project(project.id)
    assert cancelled
    assert all(job.state.value == "cancelled" for job in cancelled)
    assert components.db.get_project(project.id).state == ProjectState.PAUSED
    assert components.worker.run_once() is None


def test_cross_scene_artifact_selection_is_rejected(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Keep scene artifacts isolated", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    scenes = components.db.list_scenes(project.id)
    first_artifacts = components.db.list_artifacts(project.id, scenes[0].id)
    second_artifacts = components.db.list_artifacts(project.id, scenes[1].id)
    start = next(a for a in first_artifacts if a.metadata.get("role") == "start")
    wrong_end = next(a for a in second_artifacts if a.metadata.get("role") == "end")
    with pytest.raises(ValueError, match="does not belong"):
        components.orchestrator.approve_keyframes(
            scenes[0].id,
            ArtifactSelection(
                start_artifact_id=start.id,
                end_artifact_id=wrong_end.id,
            ),
            ApprovalRequest(approved=True),
        )


def test_keyframe_roles_cannot_be_swapped(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Bind the correct start and end frames", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    scene = components.db.list_scenes(project.id)[0]
    artifacts = components.db.list_artifacts(project.id, scene.id)
    start_1 = next(
        item for item in artifacts
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "start"
    )
    end_1 = next(
        item for item in artifacts
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "end"
    )
    assert not any(item.metadata.get("candidate") == 2 for item in artifacts)

    with pytest.raises(ValueError, match="not a start keyframe"):
        components.orchestrator.approve_keyframes(
            scene.id,
            ArtifactSelection(start_artifact_id=end_1.id, end_artifact_id=start_1.id),
            ApprovalRequest(approved=True),
        )
    components.orchestrator.approve_keyframes(
        scene.id,
        ArtifactSelection(start_artifact_id=start_1.id, end_artifact_id=end_1.id),
        ApprovalRequest(approved=True),
    )
    video_job = components.orchestrator.queue_video(scene.id)
    binding = video_job.payload["keyframe_binding"]
    assert binding["start"]["artifact_id"] == start_1.id
    assert binding["end"]["artifact_id"] == end_1.id
    assert binding["start"]["sha256"] == start_1.sha256
    assert binding["end"]["sha256"] == end_1.sha256


def test_user_research_sources_are_persisted(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(
            idea="A factual subject",
            duration_seconds=10,
            research_required=True,
            source_urls=["https://example.com/source"],
        )
    )
    sources = components.db.list_research_sources(project.id)
    assert sources[0]["url"] == "https://example.com/source"


def test_connected_scenes_share_exact_frames_and_references(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A recurring worker crosses one continuous platform", duration_seconds=10)
    )
    storyboard = components.orchestrator.plan_project(project.id)
    assert storyboard.requires_character_sheet is True
    assert storyboard.scenes[0].starts_new_continuity is True
    assert all(
        scene.continuity_group == storyboard.scenes[0].continuity_group
        for scene in storyboard.scenes
    )
    approve_storyboard_and_references(components, project.id)
    assert components.db.list_jobs(project.id)[0].kind == JobKind.CANONICAL_ASSET
    components.worker.run_until_idle()

    scenes = components.db.list_scenes(project.id)
    first_ends = components.orchestrator.db.list_artifacts(project.id, scenes[0].id)
    first_end = next(
        item for item in first_ends
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "end"
    )
    second_start = next(
        item for item in components.db.list_artifacts(project.id, scenes[1].id)
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "start"
    )
    assert second_start.metadata["shared_from_artifact_id"] == first_end.id
    assert second_start.path == first_end.path
    assert second_start.sha256 == first_end.sha256

    second_end = next(
        item for item in components.db.list_artifacts(project.id, scenes[1].id)
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "end"
    )
    with pytest.raises(ValueError, match="previous connected scene"):
        components.orchestrator.approve_keyframes(
            scenes[1].id,
            ArtifactSelection(
                start_artifact_id=second_start.id,
                end_artifact_id=second_end.id,
            ),
            ApprovalRequest(approved=True),
        )
    first_start = next(
        item for item in first_ends
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "start"
    )
    components.orchestrator.approve_keyframes(
        scenes[0].id,
        ArtifactSelection(start_artifact_id=first_start.id, end_artifact_id=first_end.id),
        ApprovalRequest(approved=True),
    )
    approved = components.orchestrator.approve_keyframes(
        scenes[1].id,
        ArtifactSelection(start_artifact_id=second_start.id, end_artifact_id=second_end.id),
        ApprovalRequest(approved=True),
    )
    assert components.db.get_artifact(approved.selected_start_artifact_id).path == first_end.path


def test_guided_keyframe_retry_requires_revision_then_persists_prompts(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A waiter carries one bowl", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    scene = components.db.list_scenes(project.id)[0]

    current = components.orchestrator.get_retry_prompts(scene.id, "keyframe")
    assert current.end_prompt
    proposal = components.orchestrator.revise_retry_prompts(
        scene.id,
        "keyframe",
        RetryPromptRevisionRequest(
            stage="keyframe",
            feedback="Keep the bowl in the waiter's right hand",
            start_prompt=current.start_prompt,
            end_prompt=current.end_prompt,
            negative_prompt=current.negative_prompt,
        ),
    )
    assert "right hand" in proposal.end_prompt

    retry = components.orchestrator.regenerate_keyframes(
        scene.id,
        RetryRequest(
            changed_start_prompt=proposal.start_prompt,
            changed_end_prompt=proposal.end_prompt,
            changed_negative_prompt=proposal.negative_prompt,
            feedback="Keep the bowl in the waiter's right hand",
        ),
    )
    assert retry.payload["end_prompt_override"] == proposal.end_prompt
    assert retry.payload["retry_feedback"].startswith("Keep the bowl")
    components.worker.run_until_idle()
    attempt = next(
        item for item in components.db.list_attempts(project.id)
        if item["job_id"] == retry.id
    )
    assert attempt["parameters"]["end_prompt_override"] == proposal.end_prompt



def test_cancelled_keyframe_job_can_retry_or_approve_existing_pair(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A courier pauses under one streetlight", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()

    scene = components.db.list_scenes(project.id)[0]
    artifacts = components.db.list_artifacts(project.id, scene.id)
    start = next(item for item in artifacts if item.metadata.get("role") == "start")
    end = next(item for item in artifacts if item.metadata.get("role") == "end")

    first_retry = components.orchestrator.regenerate_keyframes(scene.id)
    components.db.cancel_project_jobs(project.id)
    assert components.db.get_job(first_retry.id).state.value == "cancelled"

    second_retry = components.orchestrator.regenerate_keyframes(scene.id)
    assert second_retry.state.value == "queued"
    components.db.cancel_project_jobs(project.id)

    approved = components.orchestrator.approve_keyframes(
        scene.id,
        ArtifactSelection(start_artifact_id=start.id, end_artifact_id=end.id),
        ApprovalRequest(approved=True),
    )
    assert approved.state == SceneState.KEYFRAMES_APPROVED

def test_keyframe_regeneration_stops_and_rebuilds_connected_chain(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A recurring worker crosses one continuous platform", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    scenes = components.db.list_scenes(project.id)
    assert all(scene.state == SceneState.KEYFRAMES_READY for scene in scenes)

    retry = components.orchestrator.regenerate_keyframes(scenes[0].id)
    reset = components.db.list_scenes(project.id)
    assert retry.kind == JobKind.KEYFRAME
    assert reset[0].state == SceneState.KEYFRAMES_QUEUED
    assert all(scene.state == SceneState.PLANNED for scene in reset[1:])
    assert all(scene.continuity_stale for scene in reset[1:])

    components.worker.run_until_idle()
    stopped = components.db.list_scenes(project.id)
    assert stopped[0].state == SceneState.KEYFRAMES_READY
    assert all(scene.state == SceneState.PLANNED for scene in stopped[1:])

    retry_attempt = next(
        attempt for attempt in components.db.list_attempts(project.id)
        if attempt["job_id"] == retry.id
    )
    regenerated = [
        artifact for artifact in components.db.list_artifacts(project.id, stopped[0].id)
        if artifact.attempt_id == retry_attempt["id"]
    ]
    start = next(
        artifact for artifact in regenerated
        if artifact.metadata.get("candidate") == 1 and artifact.metadata.get("role") == "start"
    )
    selected_end = next(
        artifact for artifact in regenerated
        if artifact.metadata.get("candidate") == 1 and artifact.metadata.get("role") == "end"
    )
    components.orchestrator.approve_keyframes(
        stopped[0].id,
        ArtifactSelection(start_artifact_id=start.id, end_artifact_id=selected_end.id),
        ApprovalRequest(approved=True),
    )
    assert components.db.get_scene(stopped[1].id).state == SceneState.KEYFRAMES_QUEUED

    components.worker.run_until_idle()
    next_scene = components.db.get_scene(stopped[1].id)
    assert next_scene.state == SceneState.KEYFRAMES_READY
    latest_job = [
        job for job in components.db.list_jobs(project.id)
        if job.scene_id == next_scene.id and job.kind == JobKind.KEYFRAME
    ][-1]
    next_attempt = next(
        attempt for attempt in components.db.list_attempts(project.id)
        if attempt["job_id"] == latest_job.id
    )
    starts = [
        artifact for artifact in components.db.list_artifacts(project.id, next_scene.id)
        if artifact.attempt_id == next_attempt["id"] and artifact.metadata.get("role") == "start"
    ]
    assert len(starts) == 1
    assert all(item.metadata["shared_from_artifact_id"] == selected_end.id for item in starts)
    assert all(item.path == selected_end.path for item in starts)


def test_continuity_break_creates_a_fresh_flux_edit_reference_anchor(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A platform scene followed by a different train interior", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    scenes = components.db.list_scenes(project.id)
    assert len(scenes) >= 3
    for index, scene in enumerate(scenes[2:], 2):
        updated = scene.spec.model_copy(
            update={
                "continuity_group": "train-interior",
                "starts_new_continuity": index == 2,
                "continuity_reason": "New interior location" if index == 2 else "Same train interior",
                "transition_from_previous": "cut" if index == 2 else "continuous",
            }
        )
        components.db.update_scene_spec(scene.id, updated)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle()
    scenes = components.db.list_scenes(project.id)
    prior_end = next(
        item for item in components.db.list_artifacts(project.id, scenes[1].id)
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "end"
    )
    reset_start = next(
        item for item in components.db.list_artifacts(project.id, scenes[2].id)
        if item.metadata.get("candidate") == 1 and item.metadata.get("role") == "start"
    )
    assert reset_start.metadata["generation_method"] == "flux_klein_edit_reference_anchor"
    assert "shared_from_artifact_id" not in reset_start.metadata
    assert reset_start.path != prior_end.path



def test_individual_canonical_asset_retry_is_limited_to_reference_review(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A waiter serves one elderly diner", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    jobs = components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
    assert jobs
    components.worker.run_until_idle(max_jobs=20)
    asset = components.db.list_canonical_assets(project.id)[0]
    original_artifact_id = asset.artifact_id
    bundle = components.orchestrator.get_canonical_asset_retry_prompt(asset.id)
    proposal = components.orchestrator.revise_canonical_asset_prompt(
        asset.id,
        CanonicalAssetPromptRevisionRequest(
            prompt=bundle["prompt"],
            feedback="Keep this asset isolated with no unrelated people",
        ),
    )
    retry = components.orchestrator.retry_canonical_asset(
        asset.id,
        CanonicalAssetRetryRequest(
            prompt=proposal.prompt,
            feedback="Keep this asset isolated with no unrelated people",
            revision_rationale=proposal.rationale,
        ),
    )
    reset = components.db.get_canonical_asset(asset.id)
    assert retry.kind == JobKind.CANONICAL_ASSET
    assert reset.artifact_id is None
    assert reset.prompt == proposal.prompt

    components.worker.run_until_idle(max_jobs=20)
    regenerated = components.db.get_canonical_asset(asset.id)
    assert regenerated.artifact_id
    assert regenerated.artifact_id != original_artifact_id

    components.orchestrator.approve_canonical_assets(
        project.id, ApprovalRequest(approved=True)
    )
    with pytest.raises(ValueError, match="reference review"):
        components.orchestrator.retry_canonical_asset(
            asset.id, CanonicalAssetRetryRequest(prompt=proposal.prompt)
        )


def test_keyframes_can_be_reopened_while_video_is_queued(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A waiter crosses the dining room", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle(max_jobs=30)
    approve_keyframes(components, project.id)

    scenes = components.db.list_scenes(project.id)
    target = next(scene for scene in scenes if scene.state == SceneState.VIDEO_QUEUED)
    queued_video = next(
        job for job in reversed(components.db.list_jobs(project.id))
        if job.scene_id == target.id and job.kind == JobKind.VIDEO
    )
    retry = components.orchestrator.regenerate_keyframes(
        target.id,
        RetryRequest(changed_end_prompt="Corrected motion-ready closing composition"),
    )

    assert components.db.get_job(queued_video.id).state.value == "cancelled"
    reopened = components.db.get_scene(target.id)
    assert retry.kind == JobKind.KEYFRAME
    assert reopened.state == SceneState.KEYFRAMES_QUEUED
    assert reopened.selected_start_artifact_id is None
    assert reopened.selected_end_artifact_id is None


def test_approved_video_can_be_retried_and_reopens_final_review(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="A server completes a quiet service", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    approve_storyboard_and_references(components, project.id)
    components.worker.run_until_idle(max_jobs=30)
    approve_keyframes(components, project.id)
    components.worker.run_until_idle(max_jobs=50)

    scenes = components.db.list_scenes(project.id)
    for scene in scenes:
        components.orchestrator.approve_scene(
            scene.id, ApprovalRequest(approved=True)
        )
    assert components.db.get_project(project.id).state == ProjectState.FINAL_REVIEW

    target = components.db.list_scenes(project.id)[0]
    retry = components.orchestrator.queue_video(
        target.id,
        RetryRequest(changed_prompt="Preserve the approved beat with clearer hand motion"),
    )
    assert retry.kind == JobKind.VIDEO
    assert components.db.get_scene(target.id).state == SceneState.VIDEO_QUEUED
    assert components.db.get_project(project.id).state == ProjectState.SCENES
