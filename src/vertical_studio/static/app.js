const state={
  projects:[],
  current:null,
  detail:null,
  detailSignature:null,
  historyOpen:false,
  poll:null,
  runtime:null
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

async function refreshProjects(){
  state.projects=await api("/api/projects");
  q("#projects").innerHTML=state.projects.map(project=>`
    <button class="project-link ${project.id===state.current?"active":""}" data-project="${project.id}">
      <b>${esc(project.title)}</b><br>
      <small>${project.state}</small>
    </button>
  `).join("");
  document.querySelectorAll("[data-project]").forEach(
    button=>button.onclick=()=>openProject(button.dataset.project)
  );
}

async function openProject(id){
  state.current=id;
  state.detailSignature=null;
  state.historyOpen=false;
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
  return state.detail.artifacts.find(artifact=>
    artifact.scene_id===scene.id&&
    artifact.kind===kind&&
    (!role||artifact.metadata.role===role)&&
    (candidate===null||artifact.metadata.candidate===candidate)
  );
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
    project,scenes,artifacts,jobs,attempts=[],
    qa_reports=[],approvals=[],render_manifests=[],events=[]
  }=state.detail;

  q("#empty").hidden=true;
  q("#project-view").hidden=false;

  const actions=[];
  if(project.state==="draft"||project.state==="planning"){
    actions.push('<button class="primary" data-action="plan">Plan storyboard</button>');
  }
  if(project.state==="storyboard_review"){
    actions.push('<button class="primary" data-action="approve-story">Approve storyboard</button>');
    actions.push('<button data-action="regenerate-story">Regenerate storyboard</button>');
    actions.push('<button data-action="edit-project">Edit project</button>');
  }
  if(project.state==="final_review"){
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
      controls=`${pairs.map(pair=>`
        <button data-kf="${scene.id}" data-start="${pair.start.id}" data-end="${pair.end.id}">
          Approve candidate ${pair.candidate}
        </button>
      `).join("")}<button data-retry-kf="${scene.id}">Regenerate</button>`;
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
        <p class="spec">${esc(scene.spec.narrative_purpose)}</p>
        ${media}${qaHtml}
        <div class="card-actions">${controls}</div>
        ${renderSceneHistory(scene,attempts,artifacts)}
      </article>
    `;
  }).join("");

  q("#project-view").innerHTML=`
    <div class="project-header">
      <div><span class="eyebrow">9:16 / ${project.duration_seconds} SECONDS</span><h2>${esc(project.title)}</h2><p>${esc(project.idea)}</p></div>
      <span class="badge ${project.state==="complete"?"ok":""}">${project.state.replaceAll("_"," ")}</span>
    </div>
    <div class="toolbar">${actions.join("")}</div>
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
}

function bindActions(){
  document.querySelectorAll("[data-action]").forEach(button=>button.onclick=()=>act(button.dataset.action));
  document.querySelectorAll("[data-kf]").forEach(button=>button.onclick=()=>approveKeyframes(button));
  document.querySelectorAll("[data-retry-kf]").forEach(button=>button.onclick=()=>post(`/api/scenes/${button.dataset.retryKf}/retry-keyframes`,{}));
  document.querySelectorAll("[data-approve-video]").forEach(button=>button.onclick=()=>post(`/api/scenes/${button.dataset.approveVideo}/approval`,{approved:true,notes:""}));
  document.querySelectorAll("[data-retry-video]").forEach(button=>button.onclick=()=>post(`/api/scenes/${button.dataset.retryVideo}/retry-video`,{new_seed:true}));
  document.querySelectorAll("[data-edit-scene]").forEach(button=>button.onclick=()=>editScene(button.dataset.editScene));
  document.querySelectorAll("[data-delete-scene]").forEach(button=>button.onclick=()=>deleteScene(button.dataset.deleteScene));
  const history=q("#history-panel");
  if(history)history.ontoggle=()=>{state.historyOpen=history.open};
}

async function mutate(path,method,body=null){
  try{
    const options={method};
    if(body!==null)options.body=JSON.stringify(body);
    const result=await api(path,options);
    toast("Saved");
    state.detailSignature=null;
    return result;
  }catch(error){
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
    state.current=project.id;
    await post(`/api/projects/${project.id}/plan`,{});
    await refreshProjects();
    await openProject(project.id);
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
