from __future__ import annotations

from .schemas import ProjectState, SceneState


PROJECT_TRANSITIONS: dict[ProjectState, set[ProjectState]] = {
    ProjectState.DRAFT: {ProjectState.REFERENCE_ANALYSIS, ProjectState.RESEARCH_NEEDED, ProjectState.PLANNING, ProjectState.PAUSED},
    ProjectState.REFERENCE_ANALYSIS: {ProjectState.REFERENCE_REVIEW, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.REFERENCE_REVIEW: {ProjectState.STORYBOARD_REVIEW, ProjectState.REFERENCE_ANALYSIS, ProjectState.PAUSED},
    ProjectState.RESEARCH_NEEDED: {ProjectState.PLANNING, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.PLANNING: {ProjectState.STORYBOARD_REVIEW, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.STORYBOARD_REVIEW: {ProjectState.REFERENCES, ProjectState.PLANNING, ProjectState.REFERENCE_REVIEW, ProjectState.PAUSED},
    ProjectState.REFERENCES: {ProjectState.CONTACT_SHEET_REVIEW, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.CONTACT_SHEET_REVIEW: {ProjectState.SCENES, ProjectState.REFERENCES, ProjectState.PAUSED},
    ProjectState.SCENES: {ProjectState.FINAL_REVIEW, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.FINAL_REVIEW: {ProjectState.RENDERING, ProjectState.SCENES, ProjectState.PAUSED},
    ProjectState.RENDERING: {ProjectState.COMPLETE, ProjectState.FAILED, ProjectState.PAUSED},
    ProjectState.COMPLETE: {ProjectState.RENDERING},
    ProjectState.FAILED: {ProjectState.PAUSED, ProjectState.PLANNING, ProjectState.SCENES},
    ProjectState.PAUSED: {
        ProjectState.DRAFT,
        ProjectState.REFERENCE_ANALYSIS,
        ProjectState.REFERENCE_REVIEW,
        ProjectState.RESEARCH_NEEDED,
        ProjectState.PLANNING,
        ProjectState.STORYBOARD_REVIEW,
        ProjectState.REFERENCES,
        ProjectState.CONTACT_SHEET_REVIEW,
        ProjectState.SCENES,
        ProjectState.FINAL_REVIEW,
        ProjectState.RENDERING,
    },
}

SCENE_TRANSITIONS: dict[SceneState, set[SceneState]] = {
    SceneState.PLANNED: {SceneState.KEYFRAMES_QUEUED, SceneState.FAILED},
    SceneState.KEYFRAMES_QUEUED: {SceneState.KEYFRAMES_READY, SceneState.FAILED},
    SceneState.KEYFRAMES_READY: {SceneState.KEYFRAMES_APPROVED, SceneState.KEYFRAMES_QUEUED, SceneState.FAILED},
    SceneState.KEYFRAMES_APPROVED: {SceneState.VIDEO_QUEUED, SceneState.KEYFRAMES_QUEUED, SceneState.FAILED},
    SceneState.VIDEO_QUEUED: {SceneState.VIDEO_READY, SceneState.QA_FAILED, SceneState.FAILED},
    SceneState.VIDEO_READY: {SceneState.APPROVED, SceneState.QA_FAILED, SceneState.VIDEO_QUEUED, SceneState.KEYFRAMES_QUEUED},
    SceneState.QA_FAILED: {SceneState.VIDEO_QUEUED, SceneState.KEYFRAMES_QUEUED, SceneState.APPROVED, SceneState.FAILED},
    SceneState.APPROVED: {SceneState.VIDEO_QUEUED, SceneState.KEYFRAMES_QUEUED},
    SceneState.FAILED: {SceneState.KEYFRAMES_QUEUED, SceneState.VIDEO_QUEUED},
}


class InvalidTransition(ValueError):
    pass


def validate_project_transition(current: ProjectState, target: ProjectState) -> None:
    if target == current:
        return
    if target not in PROJECT_TRANSITIONS[current]:
        raise InvalidTransition(f"project cannot transition from {current} to {target}")


def validate_scene_transition(current: SceneState, target: SceneState) -> None:
    if target == current:
        return
    if target not in SCENE_TRANSITIONS[current]:
        raise InvalidTransition(f"scene cannot transition from {current} to {target}")
