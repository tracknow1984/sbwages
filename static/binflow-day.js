'use strict';
(() => {
  document.querySelectorAll('[data-prestart-check]').forEach(select => {
    const label=select.closest('.bf-prestart-check').querySelector('[data-defect-note]');
    const update=()=>{label.hidden=select.value!=='fail';label.querySelector('textarea').required=select.value==='fail';};
    select.addEventListener('change',update);update();
  });
  const day=document.querySelector('[data-run-state]');
  const plan=document.querySelector('[data-driver-plan]');
  const overview=document.querySelector('[data-overview-state]');
  const gps=document.querySelector('[data-gps-url]');
  const navigation=document.querySelector('[data-navigation-controls]');
  if(navigation && day) {
    const link=navigation.querySelector('[data-navigation-link]');
    const form=navigation.querySelector('[data-arrival-form]');
    const button=form.querySelector('button');
    const storageKey='binflow.navigation';
    // A Maps launch changes only the next visible action. It is never proof of arrival.
    const scope=JSON.stringify([navigation.dataset.navigationScope,day.dataset.runState,
      day.dataset.runStep,day.dataset.runVersion,link.href]);
    const showArrival=()=>{
      navigation.prepend(form);
      navigation.append(link);
      button.classList.replace('secondary','primary');
      link.classList.replace('primary','secondary');
      link.querySelector('[data-navigation-label]').textContent='Open directions again';
      navigation.querySelector('[data-navigation-note]').textContent=
        'When parked at this stop, tap “'+button.textContent.trim()+'” to continue.';
    };
    try {
      const saved=JSON.parse(sessionStorage.getItem(storageKey));
      if(saved?.scope===scope && Date.now()-saved.at>=0 && Date.now()-saved.at<12*60*60*1000) showArrival();
    } catch (_) { /* Navigation still works when browser storage is unavailable. */ }
    link.addEventListener('click',event=>{
      if(event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      try {sessionStorage.setItem(storageKey,JSON.stringify({scope,at:Date.now()}));} catch (_) {}
      showArrival();
    });
  }
  plan?.addEventListener('input',event=>{if(event.target.form)event.target.form.dataset.dirty='true';});
  let polling=false;
  let refreshQueued=false;
  async function getState(url) {
    const controller=new AbortController();
    const timeout=window.setTimeout(()=>controller.abort(),10000);
    try {return await fetch(url,{headers:{Accept:'application/json'},cache:'no-store',signal:controller.signal});}
    finally {window.clearTimeout(timeout);}
  }
  async function poll() {
    if (polling || document.hidden) return;
    polling=true;
    try {
      if (plan && !plan.contains(document.activeElement) && !plan.querySelector('[data-dirty]')) {
        const response=await getState(plan.dataset.driverPlan);
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) {
          plan.innerHTML=(await response.json()).html;
          document.querySelector('[data-plan-updated]').textContent='Updated '+new Date().toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
        }
      }
      if(overview){
        const response=await getState(overview.dataset.overviewState);
        if(response.ok && response.headers.get('content-type')?.includes('application/json')){
          const data=await response.json();
          if(data.version!==overview.dataset.overviewVersion)location.reload();
          const note=document.querySelector('[data-plan-updated]');
          if(note)note.textContent='Live schedule · updated '+new Date().toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
        }
      }
      if (day && (day.dataset.runStep || ['acceptance','assigned','hold','weighing','done','planned','empty'].includes(day.dataset.runStage))) {
        const response=await getState(day.dataset.runState);
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) {
          const data=await response.json();
          if(data.stage!==day.dataset.runStage || (day.dataset.runStep && data.step_key!==day.dataset.runStep) || (day.dataset.runVersion && data.version!==day.dataset.runVersion)) location.reload();
        }
      }
      if (gps) {
        const response=await getState(gps.dataset.gpsUrl);
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) gps.innerHTML=(await response.json()).html;
      }
    } catch (_) {
      const note=document.querySelector('[data-plan-updated]');
      if(note) note.textContent='Connection interrupted — showing last saved update';
    } finally {
      polling=false;
      if(refreshQueued) {refreshQueued=false;poll();}
    }
  }
  // Refresh as soon as the driver returns from Maps, including restored mobile tabs.
  // Coalesce the focus/pageshow/visibility events emitted by the same return.
  let resumeTimer;
  const resume=()=>{
    if(document.hidden) return;
    window.clearTimeout(resumeTimer);
    resumeTimer=window.setTimeout(()=>{
      if(polling) refreshQueued=true;
      else poll();
    },100);
  };
  document.addEventListener('visibilitychange',resume);
  window.addEventListener('focus',resume);
  window.addEventListener('pageshow',resume);
  window.addEventListener('online',resume);
  window.setInterval(poll,20000);
  if(gps) poll();
})();
