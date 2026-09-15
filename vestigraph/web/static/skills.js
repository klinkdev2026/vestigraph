// Skill UI owns its dialogs and drops responses after close, navigation or project changes.
export function setupSkills({api,t,getDocument,getSelected,getProjects,getProject}) {
  const $ = id => document.getElementById(id);
  const createDialog=$("skill-create-dialog"), library=$("skill-library-dialog");
  let generation=0, did=null, pid=null, revision=0, cursor=null, before=null, current=null;
  let options=null, busy=false, dirty=false, timer=null, latestRevision=0, expanded=false, polling=false;
  const enc=encodeURIComponent;
  const status=(text,create=false)=>{$(create?"skill-create-status":"skill-library-status").textContent=text;};
  function buttons() {
    for(const id of ["skill-create-submit","skill-save","skill-publish","skill-validate","skill-generate","skill-previous","skill-latest","skill-library-refresh","skill-list-more","skill-more-versions"]) $(id).disabled=busy;
    $("skill-body").disabled=busy;
    $("skill-project").disabled=busy;
    if(current) {
      const old=current.revision!==latestRevision;
      $("skill-save").disabled=busy||old;
      $("skill-publish").disabled=busy||dirty||old||current.body_kind==="outline"||current.state==="awaiting_agent";
      $("skill-validate").disabled=busy||dirty||old||!options?.validators.length;
      $("skill-generate").disabled=busy||dirty||old||!options?.generators.length;
      $("skill-previous").disabled=busy||current.revision<=1;
      $("skill-latest").disabled=busy;
    }
  }
  async function run(fn,creation=false) {
    if(busy) return;
    const token=generation;busy=true;buttons();
    try {await fn(token);} catch(e) {if(token===generation) {status(e.message||String(e),creation);if(e.code==="SKILL_REVISION_CONFLICT")$("skill-latest").disabled=false;}}
    finally {if(token===generation){busy=false;buttons();}}
  }
  function close() {generation++;busy=false;clearInterval(timer);timer=null;}
  createDialog.addEventListener("close",()=>{if(!library.open)close();});
  library.addEventListener("close",()=>{if(!createDialog.open)close();});
  $("skill-create-close").onclick=()=>createDialog.close();
  $("skill-library-close").onclick=()=>library.close();
  window.addEventListener("hashchange",()=>{if(createDialog.open)createDialog.close();if(library.open)library.close();});
  function providerOptions(id,values) {
    $(id).replaceChildren();
    for(const item of values) {const o=document.createElement("option");o.value=item.id;
      const key="skills.provider_"+item.id;const translated=t(key);
      o.textContent=translated===key ? item.label : translated;$(id).append(o);}
  }
  async function loadVersions(token,next=false) {
    const page=await api.checkpoints(did,{limit:100,cursor:next?cursor:null});
    if(token!==generation)return;
    if(!next) {revision=page.history_revision||0;$("skill-from").replaceChildren();$("skill-to").replaceChildren();}
    if((page.history_revision||0)!==revision)throw new Error(t("skills.history_changed"));
    for(const cp of page.items) for(const id of ["skill-from","skill-to"]) {
      const o=document.createElement("option");o.value=cp.id;o.textContent=cp.title||cp.filename;$(id).append(o);
    }
    cursor=page.next_cursor;$("skill-more-versions").hidden=!cursor;
    if(!next) {
      const select=$("skill-to"), selected=getSelected();
      if([...select.options].some(o=>o.value===selected))select.value=selected;
      $("skill-from").selectedIndex=Math.min(select.selectedIndex+1,$("skill-from").options.length-1);
    }
  }
  $("skill-refine").onclick=()=>{
    if(!getDocument())return;
    did=getDocument();generation++;createDialog.showModal();$("skill-create-form").reset();status("",true);
    run(async token=>{
      if(!api.capabilities.skill_library)throw new Error(t("skills.old_service"));
      options=await api.get("/skill-options");if(token!==generation)return;
      providerOptions("skill-processing",[{id:"agent",label:t("skills.external_agent")},...options.generators]);
      await loadVersions(token);
      if(token===generation&&$("skill-to").options.length<2)status(t("skills.need_two"),true);
    },true);
  };
  $("skill-more-versions").onclick=()=>run(token=>loadVersions(token,true),true);
  $("skill-create-form").onsubmit=e=>{
    e.preventDefault();
    run(async token=>{
      const intent={};for(const key of ["title","goal","rationale","applicability","parameters","success_criteria","domain"]) intent[key]=$("skill-"+key).value;
      const mode=$("skill-processing").value;
      const item=await api.post("/documents/"+enc(did)+"/skills",{intent,source:{provider:"history-window",selection:{from_id:$("skill-from").value,to_id:$("skill-to").value,history_revision:revision}}});
      if(token!==generation)return;
      createDialog.close();
      await openLibrary(item.id,item.project_id);
      if(mode!=="agent"&&library.open&&current?.id===item.id){$("skill-generator").value=mode;await run(tok=>job("generate","skill-generator",tok));}
    },true);
  };
  async function list(token,next=false) {
    const result=await api.get("/projects/"+enc(pid)+"/skills"+(next&&before?"?before="+before:""));
    if(token!==generation)return;
    if(!next){$("skill-cards").replaceChildren();expanded=false;}else expanded=true;
    for(const item of result.items) {
      const card=document.createElement("article");card.className="card";
      const b=document.createElement("button");b.type="button";b.textContent=item.title;b.dataset.skillId=item.id;
      b.onclick=()=>run(async tok=>{if(dirty)throw new Error(t("skills.unsaved"));await load(item.id,tok);});
      const p=document.createElement("p");p.className="muted";p.textContent=t("skills.state_"+item.state)+" · v"+item.revision;
      card.append(b,p);$("skill-cards").append(card);
    }
    before=result.next_before;$("skill-list-more").hidden=!before;
    if(!$("skill-cards").children.length)status(t("skills.empty"));
  }
  function render() {
    $("skill-editor").hidden=!current;if(!current)return;
    $("skill-editor-title").textContent=current.intent.title;
    $("skill-state").textContent=t("skills.state_"+current.state)+" · v"+current.revision;
    $("skill-body").value=current.body;dirty=false;
    const source=current.sources.data;
    $("skill-source").textContent=(source.document_name||current.document_id)+" · "+t("skills.source_count",{count:source.artifacts?.length||0});
    const q=new URLSearchParams({doc:current.document_id});
    if(source.selection?.to_id)q.set("version",source.selection.to_id);
    $("skill-source-link").href="#"+q.toString();
    $("skill-evidence").textContent=JSON.stringify({explanation:current.intent,evidence:source},null,2);
    $("skill-files").textContent=t("skills.files",{count:Object.keys(current.files||{}).length});
    const verification=current.verification;
    $("skill-verification").textContent=t("skills.verification_"+verification.status)+
      (verification.reports.length ? "\n"+verification.reports.map(r=>[r.scope,r.status,r.message].filter(Boolean).join(" · ")).join("\n"):"");
    exportLink();buttons();
  }
  function exportLink() {if(current)$("skill-export").href="/api/v1/skills/"+enc(current.id)+"/export?format="+enc($("skill-exporter").value||"agent-skill")+"&revision="+current.revision;}
  async function load(id,token,version=null) {
    const item=await api.get("/skills/"+enc(id)+(version?"?revision="+version:""));
    if(token!==generation)return;
    current=item;if(!version)latestRevision=item.revision;render();
  }
  async function openLibrary(id=null,project=null) {
    generation++;busy=false;dirty=false;current=null;$("skill-editor").hidden=true;status("");
    if(!library.open)library.showModal();
    pid=project||getProject()||getProjects()[0]?.id;
    $("skill-project").replaceChildren();
    for(const p of getProjects()){const o=document.createElement("option");o.value=p.id;o.textContent=p.name;$("skill-project").append(o);}
    $("skill-project").value=pid||"";
    await run(async token=>{
      if(!api.capabilities.skill_library)throw new Error(t("skills.old_service"));
      options=await api.get("/skill-options");if(token!==generation)return;
      providerOptions("skill-generator",options.generators);providerOptions("skill-validator",options.validators);providerOptions("skill-exporter",options.exporters);
      if(!pid)throw new Error(t("skills.no_project"));
      await list(token);if(id&&token===generation)await load(id,token);
    });
    clearInterval(timer);
    if(!library.open||!api.capabilities.skill_library||!pid)return;
    timer=setInterval(async()=>{
      if(!library.open||busy||dirty||polling)return;
      const token=generation,id=current?.id,version=current?.revision;
      polling=true;
      try {
        const item=id&&version===latestRevision ? await api.get("/skills/"+enc(id)) : null;
        if(token!==generation||busy||dirty||document.activeElement===$("skill-body"))return;
        if(item&&current?.id===id&&current.revision===version&&item.revision!==version){
          current=item;latestRevision=item.revision;render();
        }
        if(!expanded&&!library.contains(document.activeElement))await list(token);
      } catch(e) {if(token===generation)status(e.message||String(e));}
      finally {polling=false;}
    },5000);
  }
  $("skills-open").onclick=()=>openLibrary();
  $("skill-project").onchange=()=>{generation++;busy=false;current=null;dirty=false;pid=$("skill-project").value;$("skill-editor").hidden=true;run(token=>list(token));};
  $("skill-library-refresh").onclick=()=>run(async token=>{if(dirty)throw new Error(t("skills.unsaved"));await list(token);if(current)await load(current.id,token);});
  $("skill-list-more").onclick=()=>run(token=>list(token,true));
  $("skill-body").oninput=()=>{dirty=true;buttons();};
  $("skill-save").onclick=()=>run(async token=>{
    const item=await api.put("/skills/"+enc(current.id),{expected_revision:current.revision,body:$("skill-body").value,files:current.files,actor:"user"});
    if(token!==generation)return;current=item;latestRevision=item.revision;render();await list(token);status(t("skills.saved"));
  });
  $("skill-publish").onclick=()=>run(async token=>{
    const item=await api.post("/skills/"+enc(current.id)+"/publish",{expected_revision:current.revision});
    if(token!==generation)return;current=item;latestRevision=item.revision;render();await list(token);status(t("skills.published"));
  });
  async function job(kind,select,token) {
    const id=current.id;
    const accepted=await api.post("/skills/"+enc(id)+"/"+kind,{expected_revision:current.revision,provider:$(select).value});
    for(;;) {
      if(token!==generation)return;
      const j=await api.job(accepted.job_id);if(token!==generation)return;
      if(j.status==="succeeded"){await load(id,token);await list(token);status(t("skills.task_done"));return;}
      if(!["queued","running"].includes(j.status))throw new Error(j.error?.message||t("skills.task_failed"));
      status(t("skills.task_running"));await new Promise(resolve=>setTimeout(resolve,500));
    }
  }
  $("skill-validate").onclick=()=>run(token=>job("validate","skill-validator",token));
  $("skill-generate").onclick=()=>run(token=>job("generate","skill-generator",token));
  $("skill-exporter").onchange=exportLink;
  $("skill-previous").onclick=()=>run(async token=>{if(dirty)throw new Error(t("skills.unsaved"));await load(current.id,token,current.revision-1);});
  $("skill-latest").onclick=()=>run(async token=>{if(dirty&&!window.confirm(t("skills.discard_changes")))return;await load(current.id,token);});
  $("skill-copy-task").onclick=()=>run(async token=>{
    const text="Complete the Vestigraph skill request "+current.id+" using the local KLink MCP tools. Start with klink.status, discover the vestigraph domain, then call vestigraph.skill with skill_id="+current.id+" and follow its next_action. Submit a draft with expected_revision="+current.revision+" after writing instructions from the frozen evidence. Preserve evidence, user intent and inference distinctions; do not publish, upload, execute attachments or replay against the live design.";
    await navigator.clipboard.writeText(text);if(token===generation)status(t("skills.copied"));
  });
}
