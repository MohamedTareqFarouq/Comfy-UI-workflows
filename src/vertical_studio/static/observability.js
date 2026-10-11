const q=selector=>document.querySelector(selector);
const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const pretty=value=>JSON.stringify(value??{},null,2);
async function api(path){
  const response=await fetch(path);
  if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail||response.statusText);
  return response.json();
}
function block(title,value){
  return `<details><summary>${esc(title)}</summary><pre>${esc(typeof value==="string"?value:pretty(value))}</pre></details>`;
}
function latest(items,predicate){return [...items].reverse().find(predicate)}
function render(detail){
  const {project,scenes,artifacts,character_bible}=detail;
  const trace=character_bible?._prompt_audit?.qwen_storyboard;
  const qwen=trace?`
    <section class="audit-panel">
      <div class="audit-heading"><div><span class="eyebrow">QWEN</span><h2>Storyboard planning</h2></div><span class="badge">${esc(trace.model||"unknown model")}</span></div>
      ${block("Exact request",trace.request)}
      ${block("Raw model response",trace.raw_response||"Simulation fallback: no raw model response")}
      ${block("Validated storyboard",trace.parsed_response)}
    </section>`:`<section class="audit-panel"><h2>Qwen audit unavailable</h2><p>This project was planned before prompt auditing was enabled. Regenerate its storyboard to create a trace.</p></section>`;
  const cards=scenes.map(scene=>{
    const sceneArtifacts=artifacts.filter(item=>item.scene_id===scene.id);
    const image=latest(sceneArtifacts,item=>item.kind==="image"&&item.metadata?.role==="end");
    const video=latest(sceneArtifacts,item=>item.kind==="video");
    const runs=image?.metadata?.runs||[];
    const selected=[scene.selected_start_artifact_id,scene.selected_end_artifact_id].filter(Boolean);
    return `
      <article class="audit-scene">
        <div class="audit-heading"><div><span class="eyebrow">SCENE ${scene.position}</span><h2>${esc(scene.spec.title)}</h2></div><span class="badge">${esc(scene.state)}</span></div>
        <div class="audit-verdict">
          <b>Qwen interpretation</b>
          <span>${esc(scene.spec.narrative_purpose)}</span>
          <span><strong>Start:</strong> ${esc(scene.spec.start_composition)}</span>
          <span><strong>End:</strong> ${esc(scene.spec.end_composition)}</span>
          <span><strong>Motion:</strong> ${esc(scene.spec.motion)}</span>
        </div>
        ${selected.length?`<div class="audit-references">${selected.map(id=>`<img src="/api/artifacts/${id}/content" alt="Selected keyframe reference">`).join("")}</div>`:""}
        ${block("Complete validated scene specification",scene.spec)}
        ${block("Resolved Flux prompts and references",runs.length?runs:"No audited Flux generation yet")}
        ${block("Resolved Wan prompt and references",video?.metadata?.prompt_audit||"No audited Wan generation yet")}
      </article>`;
  }).join("");
  q("#audit-content").innerHTML=`
    <div class="audit-project-title"><span class="eyebrow">PROJECT</span><h2>${esc(project.title)}</h2><p>${esc(project.idea)}</p></div>
    ${qwen}<div class="audit-scenes">${cards}</div>`;
}
async function loadProject(id){
  q("#audit-status").textContent="Loading...";
  try{
    const detail=await api(`/api/projects/${id}`);
    render(detail);
    q("#audit-status").textContent="Audit current";
    const url=new URL(location.href);url.searchParams.set("project",id);history.replaceState(null,"",url);
  }catch(error){q("#audit-status").textContent=error.message}
}
async function init(){
  const projects=await api("/api/projects");
  const select=q("#audit-project");
  select.innerHTML=projects.map(item=>`<option value="${item.id}">${esc(item.title)}</option>`).join("");
  const requested=new URLSearchParams(location.search).get("project");
  const id=projects.some(item=>item.id===requested)?requested:projects[0]?.id;
  if(!id){q("#audit-status").textContent="No projects";return}
  select.value=id;
  select.onchange=()=>loadProject(select.value);
  q("#audit-refresh").onclick=()=>loadProject(select.value);
  await loadProject(id);
}
init().catch(error=>q("#audit-status").textContent=error.message);
