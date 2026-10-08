from __future__ import annotations

from pathlib import Path

import pytest

from vertical_studio.schemas import (
    ApprovalRequest,
    ArtifactKind,
    ArtifactSelection,
    JobKind,
    ProjectCreate,
    ProjectState,
    RenderRequest,
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

    first = components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
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


def test_scene_video_retry_is_isolated(components) -> None:
    project = components.orchestrator.create_project(
        ProjectCreate(idea="Three connected portrait scenes", duration_seconds=10)
    )
    components.orchestrator.plan_project(project.id)
    components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
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
    components.orchestrator.approve_storyboard(
        project.id, ApprovalRequest(approved=True)
    )
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
