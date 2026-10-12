const state={
  projects:[],
  current:null,
  detail:null,
  detailSignature:null,
  historyOpen:false,
  poll:null,
  runtime:null,
  operation:null
};

const q=selector=>document.querySelector(selector);

async function api(path,options={}){
  const response=await fetch(path,{
    headers:{"Content-Type":"application/json"},
    ...options
  });
  if(!response.ok){
    const body=await response.json().catch(()=>({detail:response.statusText}));
    throw new Error(body.detail||response.statusText);
  }
  return response.status===204?null:response.json();
}

function toast(message){
  const element=q("#toast");
  element.textContent=message;
  element.classList.add("show");
  setTimeout(()=>element.classList.remove("show"),2600);
}

function esc(value){
  return String(value??"").replace(
    /[&<>"']/g,
    match=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[match])
  );
}

function when(value){
  if(!value)return "";
  return new Date(value).toLocaleString();
}

const jobLabels={
  canonical_asset:"Generating a canonical reference with Flux",
  character_sheet:"Generating the character sheet with Flux",
  keyframe:"Generating continuity keyframes with Flux",
  video:"Generating scene videos with Wan",
  qa:"Checking generated media",
  render:"Rendering the final video"
};

function projectListActivity(project){
  const map={
    draft:["ready","Ready"],
    research_needed:["waiting","Needs research"],
    planning:["working","Planning"],
    storyboard_review:["waiting","Awaiting approval"],
    references:["working","Generating keyframes"],
    contact_sheet_review:["waiting","Awaiting keyframes"],
    scenes:["working","Generating scenes"],
    final_review:["waiting","Awaiting final review"],
    rendering:["working","Rendering"],
    complete:["finished","Finished"],
    failed:["error","Errored"],
    paused:["stopped","Stopped"]
  };
  const [tone,label]=map[project.state]||["ready",project.state.replaceAll("_"," ")];
  return {tone,label};
}

function projectActivity(detail){
  if(state.operation)return state.operation;
  const {project,jobs=[],scenes=[]}=detail;
  if(scenes.length&&scenes.every(scene=>scene.state==="approved")&&
      !["rendering","complete"].includes(project.state)){
    return {tone:"waiting",title:"Waiting for you",detail:"All scenes are approved; render the final video"};
  }
  const running=[...jobs].reverse().find(job=>job.state==="running");
  if(running){
    return {tone:"working",title:"Working",detail:jobLabels[running.kind]||`Running ${running.kind}`};
  }
  const queued=jobs.filter(job=>job.state==="queued");
  if(queued.length){
    const next=queued[0];
    return {
      tone:"working",title:"Working",
      detail:`${jobLabels[next.kind]||`Queued ${next.kind}`} · ${queued.length} queued`
    };
  }
  const latest=jobs[jobs.length-1];
  const failedScene=scenes.find(scene=>scene.state==="failed");
  if(project.state==="failed"||failedScene||latest?.state==="failed"){
    const failure=[...jobs].reverse().find(job=>job.state==="failed");
    return {
      tone:"error",title:"Errored",
      detail:failure?.error_message||failure?.error_code||
        (failedScene?`${failedScene.spec.title} failed`:`Project failed`)
    };
  }
  const map={
    draft:["ready","Ready","Ready to plan the storyboard"],
    research_needed:["waiting","Waiting","Research or source material is required"],
    planning:["stopped","Stopped","Planning did not finish; press Plan storyboard to retry"],
    storyboard_review:["waiting","Waiting for you","Review, edit, regenerate, or approve the storyboard"],
    references:["waiting","Reference review","Generate or approve the canonical references before scene keyframes"],
    contact_sheet_review:["waiting","Waiting for you","Approve a keyframe pair for every scene"],
    scenes:["waiting","Waiting for you","Review and approve the generated scene videos"],
    final_review:["waiting","Waiting for you","All scenes are approved; render the final video"],
    rendering:["working","Working","Rendering the final video"],
    complete:["finished","Finished","The final video is ready"],
    paused:["stopped","Stopped","The project is paused; resume it when ready"]
  };
  const [tone,title,message]=map[project.state]||["ready","Ready",project.state.replaceAll("_"," ")];
  return {tone,title,detail:message};
}

function operationLabel(path,method){
  if(path.endsWith("/plan"))return "Planning storyboard with Qwen";
  if(path.includes("storyboard-approval"))return "Starting canonical reference generation";
  if(path.includes("canonical-assets-approval"))return "Saving canonical reference review";
  if(path.includes("retry-prompts"))return "Qwen is revising the selected stage prompts";
  if(path.includes("retry-keyframes"))return "Regenerating keyframes with Flux";
  if(path.includes("keyframe-approval"))return "Saving keyframe approval";
  if(path.includes("retry-video"))return "Regenerating the scene with Wan";
  if(path.endsWith("/render"))return "Starting the final render";
  if(path.endsWith("/pause"))return "Pausing the project";
  if(path.endsWith("/resume"))return "Resuming the project";
  if(path.endsWith("/cancel"))return "Stopping queued work";
  if(method==="DELETE")return "Removing the selected item";
  return "Saving changes";
}

function renderActivityIndicator(){
  const panel=q("#activity-panel");
  if(!panel||!state.detail)return;
  const activity=projectActivity(state.detail);
  panel.className=`activity-panel ${activity.tone}`;
  panel.innerHTML=`
    <span class="activity-dot" aria-hidden="true"></span>
    <div><b>${esc(activity.title)}</b><span>${esc(activity.detail)}</span></div>
  `;
  panel.setAttribute("aria-label",`${activity.title}: ${activity.detail}`);
  document.querySelectorAll("#project-view button").forEach(
    button=>button.disabled=Boolean(state.operation&&state.operation.tone==="working")
  );
}

async function refreshProjects(){
  state.projects=await api("/api/projects");
  q("#projects").innerHTML=state.projects.map(project=>{
    const activity=projectListActivity(project);
    return `
      <button class="project-link ${project.id===state.current?"active":""}" data-project="${project.id}">
        <b>${esc(project.title)}</b>
        <small class="project-state ${activity.tone}"><span></span>${esc(activity.label)}</small>
      </button>
    `;
  }).join("");
  document.querySelectorAll("[data-project]").forEach(
    button=>button.onclick=()=>openProject(button.dataset.project)
  );
}

async function openProject(id){
  state.current=id;
  state.detailSignature=null;
  state.historyOpen=false;
  state.operation=null;
  await refreshProjects();
  await refreshDetail();
  clearInterval(state.poll);
  state.poll=setInterval(refreshDetail,1800);
}

async function refreshDetail(){
  if(!state.current)return;
  const detail=await api(`/api/projects/${state.current}`);
  const signature=JSON.stringify(detail);
  if(signature===state.detailSignature)return;
  state.detail=detail;
  state.detailSignature=signature;
  renderDetail();
}

function artifactFor(scene,kind,role,candidate=null){
  return [...state.detail.artifacts].reverse().find(artifact=>
    artifact.scene_id===scene.id&&
    artifact.kind===kind&&
    (!role||artifact.metadata.role===role)&&
    (candidate===null||artifact.metadata.candidate===candidate)
  );
}

function renderJobLifecycle(job){
  if(!job)return "";
  const labels=["Queuing","Queued","Running","Success"];
  const indexes={queued:1,running:2,succeeded:3};
  const failed=["failed","cancelled"].includes(job.state);
  const current=failed?3:(indexes[job.state]??0);
  const steps=labels.map((label,index)=>{
    const visibleLabel=failed&&index===3
      ?(job.state==="cancelled"?"Cancelled":"Failed")
      :label;
    const stateClass=failed&&index===3
      ?"failed"
      :index<current||job.state==="succeeded"
        ?"complete"
        :index===current
          ?"current"
          :"pending";
    return '<span class="job-step '+stateClass+'"><i></i>'+visibleLabel+'</span>';
  }).join("");
  return '<div class="job-lifecycle" aria-label="Generation job '+esc(job.state)+'">'
    +steps+'<small>Job '+esc(job.id.slice(0,8))+'</small></div>';
}

function attemptMedia(attempt,artifacts){
  const media=artifacts.filter(artifact=>artifact.attempt_id===attempt.id);
  if(!media.length)return "";
  return `<div class="attempt-media">${media.map(artifact=>{
    if(artifact.kind==="video"||artifact.kind==="render"){
      return `<video controls preload="metadata" src="/api/artifacts/${artifact.id}/content"></video>`;
    }
    if(artifact.kind==="image"){
      return `<img loading="lazy" src="/api/artifacts/${artifact.id}/content" alt="Earlier generated frame">`;
    }
    return "";
  }).join("")}</div>`;
}

function renderSceneHistory(scene,attempts,artifacts){
  const values=attempts.filter(attempt=>attempt.scene_id===scene.id).reverse();
  if(!values.length)return "";
  return `
    <details class="scene-history">
      <summary>Previous runs (${values.length})</summary>
      <div class="attempt-list">
        ${values.map(attempt=>`
          <div class="attempt-row">
            <div>
              <b>${esc(attempt.job_kind)} attempt ${attempt.ordinal}</b>
              <span class="badge ${attempt.status==="succeeded"?"ok":""}">${esc(attempt.status)}</span>
              <small>${when(attempt.created_at)} / seed ${attempt.seed} / ${esc(attempt.workflow_version)}</small>
              ${attempt.error?`<small class="error">${esc(attempt.error.message||JSON.stringify(attempt.error))}</small>`:""}
            </div>
            ${attemptMedia(attempt,artifacts)}
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function renderOutput(artifacts,renderManifests){
  const outputs=artifacts.filter(artifact=>artifact.kind==="render");
  if(!outputs.length)return "";
  const latest=outputs[outputs.length-1];
  const manifest=[...renderManifests].reverse().find(item=>item.artifact_id===latest.id);
  const probe=manifest?.manifest?.probe||latest.metadata||{};
  const duration=Number(probe?.format?.duration||0);
  return `
    <section class="output-panel">
      <div class="section-title">
        <div><span class="eyebrow">PUBLISHABLE OUTPUT</span><h2>Final video</h2></div>
        <a class="button-link primary-link" href="/api/artifacts/${latest.id}/content" download>Download MP4</a>
      </div>
      <div class="output-layout">
        <video class="output-player" controls preload="metadata" src="/api/artifacts/${latest.id}/content"></video>
        <div class="output-meta">
          <b>H.264 portrait export</b>
          <span>${duration?duration.toFixed(1)+" seconds":"Completed render"}</span>
          <span>Created ${when(latest.created_at)}</span>
          <small>SHA-256: ${esc(latest.sha256)}</small>
          ${outputs.length>1?`
            <details>
              <summary>Previous exports (${outputs.length-1})</summary>
              ${outputs.slice(0,-1).reverse().map((artifact,index)=>`
                <a class="history-link" href="/api/artifacts/${artifact.id}/content" download>
                  Export ${outputs.length-index-1} / ${when(artifact.created_at)}
                </a>
              `).join("")}
            </details>
          `:""}
        </div>
      </div>
    </section>
  `;
}

function renderProjectHistory(jobs,attempts,events,approvals){
  return `
    <details id="history-panel" class="history-panel" ${state.historyOpen?"open":""}>
      <summary>
        <span><b>Project history</b><small>${jobs.length} jobs / ${attempts.length} attempts / ${events.length} events</small></span>
      </summary>
      <div class="history-columns">
        <section>
          <h3>Jobs</h3>
          <div class="history-list">
            ${[...jobs].reverse().map(job=>`
              <div class="history-item">
                <span class="badge ${job.state==="succeeded"?"ok":""}">${esc(job.state)}</span>
                <div><b>${esc(job.kind)}</b><small>${when(job.created_at)} / submissions ${job.attempts}</small>
                ${job.error_message?`<small class="error">${esc(job.error_message)}</small>`:""}</div>
              </div>
            `).join("")||"<small>No jobs yet.</small>"}
          </div>
        </section>
        <section>
          <h3>Approvals</h3>
          <div class="history-list">
            ${[...approvals].reverse().map(approval=>`
              <div class="history-item">
                <span class="badge ${approval.approved?"ok":""}">${approval.approved?"approved":"rejected"}</span>
                <div><b>${esc(approval.gate)}</b><small>${when(approval.created_at)}</small>
                ${approval.notes?`<small>${esc(approval.notes)}</small>`:""}</div>
              </div>
            `).join("")||"<small>No approvals yet.</small>"}
          </div>
        </section>
        <section>
          <h3>Event log</h3>
          <div class="history-list event-list">
            ${[...events].reverse().slice(0,100).map(event=>`
              <div class="history-item">
                <span class="event-dot"></span>
                <div><b>${esc(event.event_type)}</b><small>${when(event.created_at)}</small></div>
              </div>
            `).join("")||"<small>No events yet.</small>"}
          </div>
        </section>
      </div>
    </details>
  `;
}

function renderDetail(){
  const {
    project,scenes,artifacts,jobs,attempts=[],canonical_assets=[],
    qa_reports=[],approvals=[],render_manifests=[],events=[]
  }=state.detail;

  q("#empty").hidden=true;
  q("#project-view").hidden=false;

  const actions=[];
  const allScenesApproved=scenes.length&&scenes.every(scene=>scene.state==="approved");
  if(project.state==="draft"||project.state==="planning"){
    actions.push('<button class="primary" data-action="plan">Plan storyboard</button>');
  }
  if(project.state==="storyboard_review"){
    actions.push('<button class="primary" data-action="approve-story">Approve storyboard</button>');
    actions.push('<button data-action="regenerate-story">Regenerate storyboard</button>');
    actions.push('<button data-action="edit-project">Edit project</button>');
  }
  const referencesReady=canonical_assets.length&&canonical_assets.every(asset=>asset.artifact_id);
  const referencesApproved=canonical_assets.length&&canonical_assets.every(asset=>asset.approved);
  if(project.state==="references"&&referencesReady&&!referencesApproved){
    actions.push('<button class="primary" data-action="approve-references">Approve references</button>');
    actions.push('<button data-action="regenerate-references">Regenerate references</button>');
  }
  if(project.state==="final_review"||(
      allScenesApproved&&!["rendering","complete"].includes(project.state))){
    actions.push('<button class="primary" data-action="render">Render final</button>');
  }
  if(project.state==="paused"){
    actions.push('<button data-action="resume">Resume</button>');
  }else if(!["complete","rendering"].includes(project.state)){
    actions.push('<button data-action="pause">Pause</button><button data-action="cancel">Cancel jobs</button>');
  }
  if(!["references","scenes","rendering"].includes(project.state)){
    actions.push('<button data-action="retry-project">Restart project</button>');
  }
  actions.push('<button class="danger" data-action="delete-project">Delete project</button>');

  const scenesHtml=scenes.map(scene=>{
    const sceneArtifacts=artifacts.filter(artifact=>artifact.scene_id===scene.id);
    const latestGenerationJob=[...jobs].reverse().find(
      job=>job.scene_id===scene.id&&["keyframe","video"].includes(job.kind)
    );
    const jobLifecycle=renderJobLifecycle(latestGenerationJob);
    const selectedStart=sceneArtifacts.find(a=>a.id===scene.selected_start_artifact_id)||artifactFor(scene,"image","start",1);
    const selectedEnd=sceneArtifacts.find(a=>a.id===scene.selected_end_artifact_id)||artifactFor(scene,"image","end",1);
    const video=sceneArtifacts.find(a=>a.id===scene.selected_video_artifact_id)||[...sceneArtifacts].reverse().find(a=>a.kind==="video");
    const pairs=[1,2].map(candidate=>({
      candidate,
      start:artifactFor(scene,"image","start",candidate),
      end:artifactFor(scene,"image","end",candidate)
    })).filter(pair=>pair.start&&pair.end);
    const shown=scene.state==="keyframes_ready"?pairs.flatMap(pair=>[
      {artifact:pair.start,label:`C${pair.candidate} Start`},
      {artifact:pair.end,label:`C${pair.candidate} End`}
    ]):[
      {artifact:selectedStart,label:"Selected start"},
      {artifact:selectedEnd,label:"Selected end"}
    ].filter(item=>item.artifact);

    let media=shown.length?`<div class="media-grid">${shown.map(item=>`
      <div class="media">
        <img src="/api/artifacts/${item.artifact.id}/content">
        <small>${item.label}</small>
      </div>
    `).join("")}</div>`:"";
    if(video){
      media+=`<div class="media"><video controls preload="metadata" src="/api/artifacts/${video.id}/content"></video></div>`;
    }

    const previousScene=scenes[scenes.indexOf(scene)-1];
    const waitingForPrevious=!scene.spec.starts_new_continuity&&
      previousScene?.spec.continuity_group===scene.spec.continuity_group&&
      previousScene?.state!=="keyframes_approved";
    const qaReport=[...qa_reports].reverse().find(report=>report.scene_id===scene.id);
    const qaHtml=qaReport?`
      <div class="qa ${qaReport.report.technical_pass?"pass":"fail"}">
        <b>QA: ${qaReport.report.technical_pass?"technical pass":"technical failure"}</b>
        <span>${qaReport.report.advisory_pass?"visual review passed":"visual review needs attention"}</span>
        ${(qaReport.report.reasons||[]).map(reason=>`<small>${esc(reason)}</small>`).join("")}
      </div>
    `:"";

    let controls="";
    if(project.state==="storyboard_review"&&scene.state==="planned"){
      controls=`<button data-edit-scene="${scene.id}">Edit scene</button>
        <button class="danger" data-delete-scene="${scene.id}">Delete scene</button>`;
    }
    if(scene.state==="keyframes_ready"){
      controls=waitingForPrevious
        ?`<small>Approve the previous connected scene first.</small>`
        :`${pairs.map(pair=>`
          <button data-kf="${scene.id}" data-start="${pair.start.id}" data-end="${pair.end.id}">
            Approve candidate ${pair.candidate}
          </button>
        `).join("")}<button data-retry-kf="${scene.id}">Regenerate</button>`;
    }
    if(scene.state==="planned"&&scene.continuity_stale){
      controls='<small>Waiting for the regenerated previous scene to be approved.</small>';
    }
    if(["video_ready","qa_failed"].includes(scene.state)){
      controls=`<button data-approve-video="${scene.id}">Approve video</button><button data-retry-video="${scene.id}">Retry video</button>`;
    }

    return `
      <article class="card">
        <div class="scene-head">
          <h3>${esc(scene.spec.title)}</h3>
          <span class="badge ${scene.state==="approved"?"ok":""}">${scene.state.replaceAll("_"," ")}</span>
        </div>
        <div class="continuity-meta">
          <span>${scene.spec.starts_new_continuity?"Multi-reference anchor":"Exact prior-frame continuation"}</span>
          <span>Group: ${esc(scene.spec.continuity_group)}</span>
          <span>${esc(scene.spec.transition_from_previous.replaceAll("_"," "))}</span>
          <span>Environment: ${esc(scene.spec.opening_environment_key||scene.spec.environment_key||"unlocked")} → ${esc(scene.spec.closing_environment_key||scene.spec.opening_environment_key||scene.spec.environment_key||"unlocked")}</span>
        </div>
        <p class="spec">${esc(scene.spec.narrative_purpose)}</p>
        <small class="continuity-reason">${esc(scene.spec.continuity_reason)}</small>
        ${media}${jobLifecycle}${qaHtml}
        <div class="card-actions">${controls}</div>
        ${renderSceneHistory(scene,attempts,artifacts)}
      </article>
    `;
  }).join("");

  const canonicalHtml=canonical_assets.length?`
    <section class="canonical-panel">
      <div class="section-title"><div><span class="eyebrow">CANONICAL REFERENCES</span><h2>Identity and environment anchors</h2></div>
      <span>${canonical_assets.filter(asset=>asset.approved).length}/${canonical_assets.length} approved</span></div>
      <div class="canonical-grid">${canonical_assets.map(asset=>{
        const artifact=artifacts.find(item=>item.id===asset.artifact_id);
        return `<article class="canonical-card">
          ${artifact?`<img src="/api/artifacts/${artifact.id}/content" alt="${esc(asset.label)}">`:`<div class="reference-placeholder">Generating…</div>`}
          <div><span class="badge">${esc(asset.role)}</span><h3>${esc(asset.label)}</h3>
          <p>${esc(asset.description||asset.prompt)}</p><small>${esc(asset.key)}</small></div>
        </article>`;
      }).join("")}</div>
    </section>
  `:"";

  const characterSheet=[...artifacts].reverse().find(
    artifact=>artifact.metadata.role==="character_sheet"
  );
  const characterPlan=state.detail.character_bible?._generation_plan||{};
  const characterHtml=canonical_assets.length?"":characterPlan.requires_character_sheet?`
    <section class="character-plan">
      <div><span class="eyebrow">IDENTITY REFERENCE</span><h3>Character sheet required</h3>
      <p>${esc(characterPlan.character_sheet_prompt)}</p></div>
      ${characterSheet?`<img src="/api/artifacts/${characterSheet.id}/content" alt="Generated character sheet">`:
        '<span class="badge">Pending generation</span>'}
    </section>
  `:`<div class="notice">Qwen determined that this storyboard does not require a character sheet.</div>`;

  q("#project-view").innerHTML=`
    <div class="project-header">
      <div><span class="eyebrow">9:16 / ${project.duration_seconds} SECONDS</span><h2>${esc(project.title)}</h2><p>${esc(project.idea)}</p></div>
      <span class="badge ${project.state==="complete"?"ok":""}">${project.state.replaceAll("_"," ")}</span>
    </div>
    <div id="activity-panel" class="activity-panel" role="status" aria-live="polite"></div>
    <div class="toolbar">${actions.join("")}</div>
    ${canonicalHtml}
    ${characterHtml}
    <div class="notice">${project.execution_mode==="real"
      ?"Real mode: Qwen and ComfyUI models are loaded one at a time to protect GPU memory."
      :"Simulation mode creates deterministic portrait fixtures."}
      Queue: ${jobs.filter(job=>["queued","running"].includes(job.state)).length} active.</div>
    ${renderOutput(artifacts,render_manifests)}
    <div class="section-title"><h2>Scenes</h2><span>${scenes.length} scenes</span></div>
    <div class="scene-grid">${scenesHtml||"<p>Plan the storyboard to create scenes.</p>"}</div>
    ${renderProjectHistory(jobs,attempts,events,approvals)}
  `;
  bindActions();
  renderActivityIndicator();
}

function bindActions(){
  document.querySelectorAll("[data-action]").forEach(button=>button.onclick=()=>act(button.dataset.action));
  document.querySelectorAll("[data-kf]").forEach(button=>button.onclick=()=>approveKeyframes(button));
  document.querySelectorAll("[data-retry-kf]").forEach(button=>button.onclick=()=>openGuidedRetry(button.dataset.retryKf,"keyframe",button));
  document.querySelectorAll("[data-approve-video]").forEach(button=>button.onclick=()=>post(`/api/scenes/${button.dataset.approveVideo}/approval`,{approved:true,notes:""}));
  document.querySelectorAll("[data-retry-video]").forEach(button=>button.onclick=()=>openGuidedRetry(button.dataset.retryVideo,"video",button));
  document.querySelectorAll("[data-edit-scene]").forEach(button=>button.onclick=()=>editScene(button.dataset.editScene));
  document.querySelectorAll("[data-delete-scene]").forEach(button=>button.onclick=()=>deleteScene(button.dataset.deleteScene));
  const history=q("#history-panel");
  if(history)history.ontoggle=()=>{state.historyOpen=history.open};
}

async function mutate(path,method,body=null){
  state.operation={tone:"working",title:"Working",detail:operationLabel(path,method)};
  renderActivityIndicator();
  try{
    const options={method};
    if(body!==null)options.body=JSON.stringify(body);
    const result=await api(path,options);
    toast("Saved");
    state.operation=null;
    state.detailSignature=null;
    renderActivityIndicator();
    return result;
  }catch(error){
    state.operation={tone:"error",title:"Errored",detail:error.message};
    renderActivityIndicator();
    toast(error.message);
    throw error;
  }
}

async function post(path,body){
  try{
    const result=await mutate(path,"POST",body);
    await refreshDetail();
    return result;
  }catch(_error){}
}

async function editScene(sceneId){
  const scene=state.detail.scenes.find(item=>item.id===sceneId);
  if(!scene)return;
  const title=prompt("Scene title",scene.spec.title);
  if(title===null)return;
  const narrative=prompt("What happens in this scene?",scene.spec.narrative_purpose);
  if(narrative===null)return;
  const start=prompt("Start composition",scene.spec.start_composition);
  if(start===null)return;
  const end=prompt("End composition",scene.spec.end_composition);
  if(end===null)return;
  const motion=prompt("Motion",scene.spec.motion);
  if(motion===null)return;
  try{
    await mutate(`/api/scenes/${sceneId}`,"PATCH",{
      ...scene.spec,title,narrative_purpose:narrative,
      start_composition:start,end_composition:end,motion
    });
    await refreshDetail();
  }catch(_error){}
}

async function deleteScene(sceneId){
  if(!confirm("Delete this scene? This cannot be undone."))return;
  try{
    await mutate(`/api/scenes/${sceneId}`,"DELETE");
    await refreshDetail();
  }catch(_error){}
}

function setRetryStatus(message,tone="working"){
  const status=q("#retry-status");
  status.textContent=message;
  status.dataset.tone=tone;
  status.hidden=!message;
}

async function openGuidedRetry(sceneId,stage,trigger=null){
  if(trigger?.disabled)return;
  const dialog=q("#retry-dialog");
  const form=q("#retry-form");
  const reviseButton=q("#revise-retry-prompts");
  const submitButton=form.querySelector('[type="submit"]');
  if(trigger)trigger.disabled=true;
  form.reset();
  form.elements.scene_id.value=sceneId;
  form.elements.stage.value=stage;
  form.elements.revision_rationale.value="";
  form.dataset.revised="false";
  form.dataset.busy="true";
  reviseButton.disabled=true;
  submitButton.disabled=true;
  q("#retry-start-field").hidden=true;
  q("#retry-end-field").hidden=stage!=="keyframe";
  q("#retry-video-field").hidden=stage!=="video";
  q("#retry-title").textContent=stage==="keyframe"?"Revise Flux keyframe prompts":"Revise Wan video prompt";
  q("#retry-description").textContent=stage==="keyframe"
    ?"Describe what is wrong with this scene's keyframes. Qwen will revise only the Flux prompts."
    :"Describe what is wrong with this clip. Qwen will revise only the Wan prompt.";
  q("#retry-rationale").hidden=true;
  setRetryStatus("Loading the current prompts...");
  if(!dialog.open)dialog.showModal();
  try{
    const bundle=await api("/api/scenes/"+sceneId+"/retry-prompts/"+stage);
    form.elements.start_prompt.value=bundle.start_prompt||"";
    form.elements.end_prompt.value=bundle.end_prompt||"";
    form.elements.video_prompt.value=bundle.video_prompt||"";
    form.elements.negative_prompt.value=bundle.negative_prompt||"";
    q("#retry-start-field").hidden=stage!=="keyframe"||!bundle.start_prompt;
    setRetryStatus("Prompts loaded. Add feedback, then ask Qwen to revise.","ready");
    reviseButton.disabled=false;
  }catch(error){
    setRetryStatus(error.message,"error");
    toast(error.message);
  }finally{
    delete form.dataset.busy;
    if(trigger)trigger.disabled=false;
  }
}

async function reviseGuidedRetry(){
  const form=q("#retry-form");
  if(form.dataset.busy==="true"||!form.reportValidity())return;
  const stage=form.elements.stage.value;
  const button=q("#revise-retry-prompts");
  const submitButton=form.querySelector('[type="submit"]');
  const original=button.textContent;
  form.dataset.busy="true";
  form.dataset.revised="false";
  button.disabled=true;
  submitButton.disabled=true;
  button.textContent="Qwen is revising...";
  setRetryStatus("Qwen is revising the selected prompts. Please wait - this can take a minute.");
  try{
    const proposal=await api(
      "/api/scenes/"+form.elements.scene_id.value+"/retry-prompts/"+stage+"/revise",
      {
        method:"POST",
        body:JSON.stringify({
          stage,
          feedback:form.elements.feedback.value,
          start_prompt:stage==="keyframe"&&form.elements.start_prompt.value?form.elements.start_prompt.value:null,
          end_prompt:stage==="keyframe"?form.elements.end_prompt.value:null,
          video_prompt:stage==="video"?form.elements.video_prompt.value:null,
          negative_prompt:form.elements.negative_prompt.value
        })
      }
    );
    form.elements.start_prompt.value=proposal.start_prompt||"";
    form.elements.end_prompt.value=proposal.end_prompt||"";
    form.elements.video_prompt.value=proposal.video_prompt||"";
    form.elements.negative_prompt.value=proposal.negative_prompt||"";
    const rationale=q("#retry-rationale");
    rationale.textContent=proposal.rationale||"Qwen's revised prompts are ready for review.";
    rationale.hidden=false;
    form.elements.revision_rationale.value=proposal.rationale||"";
    form.dataset.revised="true";
    submitButton.disabled=false;
    setRetryStatus("Revision ready. Review the prompts, then accept to queue regeneration.","ready");
  }catch(error){
    setRetryStatus(error.message,"error");
    toast(error.message);
  }finally{
    delete form.dataset.busy;
    button.disabled=false;
    button.textContent=original;
  }
}

async function approveKeyframes(button){
  await post(`/api/scenes/${button.dataset.kf}/keyframe-approval`,{
    selection:{
      start_artifact_id:button.dataset.start,
      end_artifact_id:button.dataset.end
    },
    approval:{approved:true,notes:""}
  });
}

async function act(name){
  if(name==="plan")return post(`/api/projects/${state.current}/plan`,{});
  if(name==="approve-story")return post(`/api/projects/${state.current}/storyboard-approval`,{approved:true,notes:""});
  if(name==="approve-references")return post(`/api/projects/${state.current}/canonical-assets-approval`,{approved:true,notes:""});
  if(name==="regenerate-references")return post(`/api/projects/${state.current}/canonical-assets-approval`,{approved:false,notes:"Regenerate all canonical references"});
  if(name==="regenerate-story"){
    const feedback=prompt("What should change in the regenerated storyboard?","");
    if(feedback===null)return;
    return post(`/api/projects/${state.current}/plan`,{feedback});
  }
  if(name==="edit-project"){
    const project=state.detail.project;
    const title=prompt("Project title",project.title);
    if(title===null)return;
    const idea=prompt("Story idea",project.idea);
    if(idea===null)return;
    const style=prompt("Visual style",project.visual_style);
    if(style===null)return;
    try{
      await mutate(`/api/projects/${state.current}`,"PATCH",{
        title,idea,visual_style:style
      });
      await refreshProjects();
      await refreshDetail();
    }catch(_error){}
    return;
  }
  if(name==="retry-project"){
    if(!confirm("Restart this project and remove its generated attempts?"))return;
    try{
      await mutate(`/api/projects/${state.current}/retry`,"POST",{});
      await post(`/api/projects/${state.current}/plan`,{});
      await refreshProjects();
    }catch(_error){}
    return;
  }
  if(name==="delete-project"){
    if(!confirm("Permanently delete this project and its generated files?"))return;
    try{
      await mutate(`/api/projects/${state.current}`,"DELETE");
      clearInterval(state.poll);
      state.current=null;state.detail=null;state.detailSignature=null;
      q("#project-view").hidden=true;q("#empty").hidden=false;
      await refreshProjects();
      if(state.projects[0])await openProject(state.projects[0].id);
    }catch(_error){}
    return;
  }
  if(name==="render")return post(`/api/projects/${state.current}/render`,{});
  if(name==="pause")return post(`/api/projects/${state.current}/pause`,{});
  if(name==="resume")return post(`/api/projects/${state.current}/resume`,{});
  if(name==="cancel")return post(`/api/projects/${state.current}/cancel`,{});
}


q("#revise-retry-prompts").onclick=reviseGuidedRetry;
document.querySelector("[data-retry-close]").onclick=()=>q("#retry-dialog").close();
q("#retry-form").onsubmit=async event=>{
  event.preventDefault();
  const form=event.currentTarget;
  if(form.dataset.revised!=="true"||form.dataset.busy==="true")return;
  const sceneId=form.elements.scene_id.value;
  const stage=form.elements.stage.value;
  const submitButton=form.querySelector('[type="submit"]');
  const reviseButton=q("#revise-retry-prompts");
  const original=submitButton.textContent;
  const body={
    changed_negative_prompt:form.elements.negative_prompt.value,
    feedback:form.elements.feedback.value,
    revision_rationale:form.elements.revision_rationale.value,
    new_seed:true
  };
  if(stage==="keyframe"){
    body.changed_start_prompt=form.elements.start_prompt.value||null;
    body.changed_end_prompt=form.elements.end_prompt.value;
  }else{
    body.changed_prompt=form.elements.video_prompt.value;
  }
  form.dataset.busy="true";
  submitButton.disabled=true;
  reviseButton.disabled=true;
  submitButton.textContent="Queuing regeneration...";
  setRetryStatus(
    stage==="keyframe"
      ?"Queuing the Flux keyframe retry..."
      :"Queuing the Wan video retry..."
  );
  try{
    const endpoint=stage==="keyframe"?"retry-keyframes":"retry-video";
    const job=await mutate("/api/scenes/"+sceneId+"/"+endpoint,"POST",body);
    if(!job?.id)throw new Error("The server did not confirm a regeneration job.");
    setRetryStatus("Retry job "+job.id.slice(0,8)+" queued successfully.","ready");
    if(state.detail){
      const existing=state.detail.jobs.findIndex(item=>item.id===job.id);
      if(existing>=0)state.detail.jobs[existing]=job;
      else state.detail.jobs.push(job);
      const scene=state.detail.scenes.find(item=>item.id===sceneId);
      if(scene)scene.state=stage==="keyframe"?"keyframes_queued":"video_queued";
      state.detailSignature=null;
      renderDetail();
    }
    toast((stage==="keyframe"?"Keyframe":"Video")+" regeneration queued");
    q("#retry-dialog").close();
    await refreshDetail();
  }catch(error){
    setRetryStatus(error.message,"error");
  }finally{
    delete form.dataset.busy;
    if(q("#retry-dialog").open){
      submitButton.disabled=form.dataset.revised!=="true";
      reviseButton.disabled=false;
      submitButton.textContent=original;
    }
  }
};

q("#new-project").onclick=()=>q("#create-dialog").showModal();
document.querySelector("[data-close]").onclick=()=>q("#create-dialog").close();

q("#create-form").onsubmit=async event=>{
  event.preventDefault();
  const form=new FormData(event.target);
  try{
    const project=await api("/api/projects",{
      method:"POST",
      body:JSON.stringify({
        title:form.get("title")||null,
        idea:form.get("idea"),
        duration_seconds:Number(form.get("duration_seconds")),
        visual_style:form.get("visual_style"),
        research_required:form.get("research_required")==="on",
        source_urls:String(form.get("source_urls")||"").split(/\r?\n/).map(value=>value.trim()).filter(Boolean)
      })
    });
    q("#create-dialog").close();
    event.target.reset();
    await openProject(project.id);
    await post(`/api/projects/${project.id}/plan`,{});
    await refreshProjects();
  }catch(error){
    toast(error.message);
  }
};

(async()=>{
  try{
    const status=await api("/api/status");
    state.runtime=status;
    q("#runtime").textContent=`${status.mode.toUpperCase()} / ${status.platform.toUpperCase()}${status.managed_qwen?" / AUTO GPU":""}`;
    await refreshProjects();
    if(state.projects[0])openProject(state.projects[0].id);
  }catch(error){
    q("#runtime").textContent="Backend unavailable";
    toast(error.message);
  }
})();

if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js");
