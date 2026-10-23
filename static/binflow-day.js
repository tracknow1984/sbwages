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
  plan?.addEventListener('input',event=>{if(event.target.form)event.target.form.dataset.dirty='true';});
  let polling=false;
  async function poll() {
    if (polling || document.hidden) return;
    polling=true;
    try {
      if (plan && !plan.contains(document.activeElement) && !plan.querySelector('[data-dirty]')) {
        const response=await fetch(plan.dataset.driverPlan,{headers:{Accept:'application/json'}});
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) {
          plan.innerHTML=(await response.json()).html;
          document.querySelector('[data-plan-updated]').textContent='Updated '+new Date().toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
        }
      }
      if(overview){
        const response=await fetch(overview.dataset.overviewState,{headers:{Accept:'application/json'}});
        if(response.ok && response.headers.get('content-type')?.includes('application/json')){
          const data=await response.json();
          if(data.version!==overview.dataset.overviewVersion)location.reload();
          const note=document.querySelector('[data-plan-updated]');
          if(note)note.textContent='Live schedule · updated '+new Date().toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
        }
      }
      if (day && (day.dataset.runStep || ['acceptance','assigned','hold','weighing','done','planned','empty'].includes(day.dataset.runStage))) {
        const response=await fetch(day.dataset.runState,{headers:{Accept:'application/json'}});
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) {
          const data=await response.json();
          if(data.stage!==day.dataset.runStage || (day.dataset.runStep && data.step_key!==day.dataset.runStep) || (day.dataset.runVersion && data.version!==day.dataset.runVersion)) location.reload();
        }
      }
      if (gps) {
        const response=await fetch(gps.dataset.gpsUrl,{headers:{Accept:'application/json'}});
        if(response.ok && response.headers.get('content-type')?.includes('application/json')) gps.innerHTML=(await response.json()).html;
      }
    } catch (_) {
      const note=document.querySelector('[data-plan-updated]');
      if(note) note.textContent='Connection interrupted — showing last saved update';
    } finally {polling=false;}
  }
  window.setInterval(poll,20000);
  if(gps) poll();
})();
