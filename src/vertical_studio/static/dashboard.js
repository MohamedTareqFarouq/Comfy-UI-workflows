const state = {
  projects: [],
  details: {},
  poll: null,
  runtime: null,
};

const q = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const error = new Error(body.detail || response.statusText);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function statusTone(status) {
  if (['running', 'queued', 'working', 'ready'].includes(status)) return 'working';
  if (status === 'unqualified') return 'working';
  if (status === 'unavailable') return 'error';
  if (['waiting', 'pending', 'review'].includes(status)) return 'waiting';
  if (['approved', 'complete', 'finished', 'succeeded'].includes(status)) return 'ok';
  if (['rejected', 'failed', 'error'].includes(status)) return 'error';
  return 'stopped';
}

function phaseLabel(project) {
  const map = {
    draft: 'Draft',
    reference_analysis: 'Reference analysis',
    reference_review: 'Reference review',
    storyboard_review: 'Storyboard review',
    references: 'Canonical references',
    scenes: 'Scene generation',
    final_review: 'Final review',
    rendering: 'Rendering',
    complete: 'Complete',
    paused: 'Paused',
    failed: 'Failed',
    research_needed: 'Research needed',
    planning: 'Planning',
  };
  return map[project.state] || project.state.replaceAll('_', ' ');
}

function queueSummary(detail) {
  const jobs = detail.jobs || [];
  const active = jobs.filter((job) => ['queued', 'running'].includes(job.state));
  const latest = [...jobs].reverse()[0];
  return {
    active,
    latest,
    pendingApprovals: (detail.approvals || []).filter((approval) => approval.status === 'pending'),
  };
}

async function loadProjectDetail(projectId) {
  const detail = await api(`/api/projects/${projectId}`);
  state.details[projectId] = detail;
  return detail;
}

async function refreshDashboard() {
  const [projects, runtime] = await Promise.all([
    api('/api/projects'),
    api('/api/status').catch(() => null),
  ]);
  state.projects = projects;
  state.runtime = runtime;

  await Promise.all(
    projects.map(async (project) => {
      if (!state.details[project.id]) {
        await loadProjectDetail(project.id);
      }
    })
  );

  renderOverview();
  renderRuntime();
  renderQueue();
  renderProjectBoard();
}

function renderRuntime() {
  const runtime = state.runtime;
  if (!runtime) {
    q('#runtime-readiness').textContent = 'Offline';
    q('#runtime-meta').textContent = 'Runtime status unavailable.';
    q('#capability-list').innerHTML = '';
    return;
  }

  const ready = runtime.mode === 'real' && runtime.generator?.available;
  const readiness = q('#runtime-readiness');
  readiness.className = `badge ${ready ? 'ok' : 'waiting'}`;
  readiness.textContent = ready ? 'ComfyUI connected' : `${runtime.mode} mode`;
  const qwenState = runtime.mode === 'simulation'
    ? 'not used in simulation'
    : runtime.managed_qwen
      ? 'managed on demand'
      : runtime.model?.available
        ? 'available'
        : 'unavailable';
  q('#runtime-meta').innerHTML = `
    <span>Keyframe backend: <b>${esc(runtime.keyframe_image_backend)}</b></span>
    <span>Unqualified override: <b>${runtime.allow_unqualified_capabilities ? 'enabled' : 'disabled'}</b></span>
    <span>Qwen vision: <b>${esc(qwenState)}</b></span>
    <span>Worker: <b>${runtime.worker_alive ? 'live' : 'idle'}</b></span>
  `;

  const overrideIds = new Set(runtime.unqualified_override_capabilities || []);
  q('#capability-list').innerHTML = (runtime.capabilities || []).map((capability) => {
    const stateLabel = capability.qualification_state.replaceAll('_', ' ');
    const override = overrideIds.has(capability.id);
    return `
      <div class="capability-row">
        <span>${esc(capability.id)}</span>
        <span class="badge ${statusTone(capability.qualification_state)}">${esc(stateLabel)}</span>
        ${override ? '<small>override available</small>' : ''}
      </div>
    `;
  }).join('');
}

function renderOverview() {
  const overview = q('#overview-grid');
  const total = state.projects.length;
  const trying = state.projects.filter((project) => ['draft', 'reference_analysis', 'planning', 'storyboard_review', 'references', 'scenes', 'rendering'].includes(project.state)).length;
  const waiting = state.projects.filter((project) => ['reference_review', 'storyboard_review', 'final_review', 'contact_sheet_review', 'paused', 'research_needed'].includes(project.state)).length;
  const active = state.projects.filter((project) => ['reference_analysis', 'planning', 'references', 'scenes', 'rendering'].includes(project.state)).length;

  overview.innerHTML = `
    <article class="metric-card">
      <div class="eyebrow">Projects</div>
      <h3>${total}</h3>
      <span>Total tracked sessions</span>
    </article>
    <article class="metric-card">
      <div class="eyebrow">Active now</div>
      <h3>${active}</h3>
      <span>Projects with live work in flight</span>
    </article>
    <article class="metric-card">
      <div class="eyebrow">Awaiting approval</div>
      <h3>${waiting}</h3>
      <span>Needs human decision</span>
    </article>
    <article class="metric-card">
      <div class="eyebrow">In progress</div>
      <h3>${trying}</h3>
      <span>Non-terminal pipeline states</span>
    </article>
  `;
}

function renderQueue() {
  const queue = q('#queue-panel');
  if (!state.projects.length) {
    queue.innerHTML = '<div class="empty-state">No active projects yet.</div>';
    return;
  }

  queue.innerHTML = state.projects
    .map((project) => {
      const detail = state.details[project.id] || { jobs: [], approvals: [] };
      const summary = queueSummary(detail);
      const activeCount = summary.active.length;
      const latest = summary.latest;
      return `
        <article class="queue-item">
          <div class="project-head">
            <div>
              <b>${project.title}</b>
              <small>${phaseLabel(project)}</small>
            </div>
            <span class="badge ${statusTone(project.state)}">${project.state.replaceAll('_', ' ')}</span>
          </div>
          <small>${activeCount ? `${activeCount} active jobs` : 'Waiting on next step'}</small>
          <small>${latest ? `Last event: ${latest.kind} / ${latest.state}` : 'No job activity yet'}</small>
          <small>${summary.pendingApprovals.length ? `${summary.pendingApprovals.length} approvals pending` : 'No pending approvals'}</small>
        </article>
      `;
    })
    .join('');
}

function renderProjectBoard() {
  const content = q('#dashboard-content');
  if (!state.projects.length) {
    content.innerHTML = '<div class="empty-state">The agent has no work queued yet.</div>';
    return;
  }

  content.innerHTML = state.projects
    .map((project) => {
      const detail = state.details[project.id];
      if (!detail) return '';

      const jobs = detail.jobs || [];
      const approvals = detail.approvals || [];
      const activeJobs = jobs.filter((job) => ['queued', 'running'].includes(job.state));
      const pendingApprovals = approvals.filter((approval) => approval.status === 'pending');
      const recentEvents = (detail.events || []).slice(-4).reverse();
      const scenes = detail.scenes || [];
      const currentPhase = phaseLabel(project);

      let approvalBlock = '<div class="approval-box"><b>Approval</b><small>No approval gate is active right now.</small></div>';
      if (pendingApprovals.length) {
        approvalBlock = `<div class="approval-box"><b>Approval queue</b>${pendingApprovals.map((approval) => `
          <div class="approval-item">
            <small>${approval.gate}</small>
            <div class="approval-actions">
              <button data-approve-project="${project.id}" data-gate="${approval.gate}">Approve</button>
              <button class="danger" data-reject-project="${project.id}" data-gate="${approval.gate}">Reject</button>
            </div>
          </div>
        `).join('')}</div>`;
      }

      let sceneList = '<div class="status-block"><b>Scenes</b><small>No scenes are active yet.</small></div>';
      if (scenes.length) {
        sceneList = `<div class="status-block"><b>Scenes</b><ul class="scene-list">${scenes.map((scene) => `
          <li class="scene-item">
            <span>${scene.spec.title}</span>
            <span class="badge ${statusTone(scene.state)}">${scene.state.replaceAll('_', ' ')}</span>
          </li>
        `).join('')}</ul></div>`;
      }

      return `
        <article class="project-panel">
          <div class="project-head">
            <div>
              <h3>${project.title}</h3>
              <div class="meta-row">
                <span>${project.goal_mode}</span>
                <span>•</span>
                <span>${project.source_mode}</span>
                <span>•</span>
                <span>${project.duration_seconds}s</span>
              </div>
            </div>
            <span class="badge ${statusTone(project.state)}">${currentPhase}</span>
          </div>

          <div class="project-grid">
            <div class="status-block">
              <b>Current phase</b>
              <div>${currentPhase}</div>
              <small>${project.idea}</small>
            </div>
            ${approvalBlock}
            <div class="status-block">
              <b>Live work</b>
              <div>${activeJobs.length ? activeJobs.map((job) => `<small>${job.kind} · ${job.state}</small>`).join('') : '<small>Idle / waiting for next trigger</small>'}</div>
            </div>
          </div>

          <div class="project-grid">
            ${sceneList}
            <div class="timeline-box">
              <b>Recent agent events</b>
              <ul class="timeline-list">
                ${recentEvents.length ? recentEvents.map((event) => `<li>${event.event_type}</li>`).join('') : '<li>No recent events.</li>'}
              </ul>
            </div>
          </div>
        </article>
      `;
    })
    .join('');

  bindApprovalButtons();
}

async function handleApproval(projectId, gate, approved) {
  const detail = state.details[projectId];
  if (!detail) return;

  const notes = approved ? 'Approved from agent dashboard.' : 'Rejected from agent dashboard.';
  const path = (() => {
    if (gate === 'reference_blueprint') return `/api/projects/${projectId}/reference/approval`;
    if (gate === 'storyboard') return `/api/projects/${projectId}/storyboard-approval`;
    if (gate === 'canonical_assets') return `/api/projects/${projectId}/canonical-assets-approval`;
    return null;
  })();

  if (!path) {
    const scene = detail.scenes.find((item) => item.state === 'video_ready' || item.state === 'qa_failed');
    if (!scene) return;
    await api(`/api/scenes/${scene.id}/approval`, {
      method: 'POST',
      body: JSON.stringify({ approved, notes, override_reason: approved ? '' : notes }),
    });
    return;
  }

  await api(path, {
    method: 'POST',
    body: JSON.stringify({ approved, notes }),
  });

  await refreshDashboard();
}

function bindApprovalButtons() {
  document.querySelectorAll('[data-approve-project]').forEach((button) => {
    button.onclick = async () => {
      const { approveProject, gate } = button.dataset;
      await handleApproval(approveProject, gate, true);
    };
  });

  document.querySelectorAll('[data-reject-project]').forEach((button) => {
    button.onclick = async () => {
      const { rejectProject, gate } = button.dataset;
      await handleApproval(rejectProject, gate, false);
    };
  });
}

async function bootstrap() {
  const runtime = q('#runtime');
  await refreshDashboard();
  state.poll = setInterval(() => {
    refreshDashboard().then(() => {
      const current = state.runtime;
      runtime.textContent = current
        ? `${current.mode} / ${current.worker_alive ? 'worker live' : 'worker idle'}`
        : 'offline';
    }).catch(() => { runtime.textContent = 'offline'; });
  }, 2500);
  const current = state.runtime;
  runtime.textContent = current
    ? `${current.mode} / ${current.worker_alive ? 'worker live' : 'worker idle'}`
    : 'offline';

  q('#dashboard-refresh').onclick = () => refreshDashboard();
}

bootstrap().catch((error) => {
  q('#dashboard-status').textContent = error.message;
});
