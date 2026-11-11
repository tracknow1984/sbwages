'use strict';
document.querySelectorAll('[data-truck-check]').forEach(row => {
  const field=row.querySelector('[data-issue-field]');
  if (!field) return;
  const input=field.querySelector('input, textarea');
  function update() {
    const failed=row.querySelector('input[type=radio]:checked')?.value==='fail';
    field.hidden=!failed;
    input.required=failed;
    input.disabled=!failed;
  }
  row.addEventListener('change',update);
  update();
});

const inspection = document.querySelector('[data-truck-inspection]');
if (inspection) {
  const fitness=inspection.querySelector('[name=fit_for_duty]');
  const notes=inspection.querySelector('[data-fit-notes]');
  fitness.addEventListener('change',()=>{
    if(fitness.value==='no') notes.open=true;
  });
  const key='bf.truck-inspection.'+inspection.dataset.draft;
  const parts=[...inspection.querySelectorAll('[data-inspection-part]')];
  const savedFields=[...inspection.querySelectorAll('[data-truck-check] input[type=radio], [name=reading]')];
  let step=0;
  try {
    const draft=JSON.parse(sessionStorage.getItem(key) || 'null');
    if (draft && draft.saved>Date.now()-48*3600*1000) {
      step=[0,1,2].includes(draft.step)?draft.step:0;
      for(const el of savedFields) {
        if(el.type==='radio') el.checked=draft.values[el.name]===el.value;
        else el.value=draft.values[el.name]||'';
        el.dispatchEvent(new Event('change',{bubbles:true}));
      }
      // Files and signatures require a fresh user selection after a reload.
      const missing=parts.find(part=>[...part.querySelectorAll('input:required,textarea:required')].some(el=>!el.validity.valid));
      if(missing)step=Math.min(step,Number(missing.dataset.inspectionPart));
    }
  } catch (_) {}
  const nav=document.createElement('div');
  nav.className='bf-inspection-nav';
  const back=document.createElement('button'), next=document.createElement('button'), progress=document.createElement('span');
  back.type=next.type='button'; back.className='secondary'; next.className='primary';
  back.textContent='Back'; next.textContent='Next'; progress.setAttribute('role','status');
  nav.append(back,progress,next);inspection.append(nav);
  function save(){
    const values={};
    for(const el of savedFields) if(el.type!=='radio'||el.checked) values[el.name]=el.value;
    try{sessionStorage.setItem(key,JSON.stringify({step,values,saved:Date.now()}));}catch(_){}
  }
  function show(move=false) {
    for(const part of parts)part.hidden=Number(part.dataset.inspectionPart)!==step;
    back.hidden=step===0;next.hidden=step===2;
    progress.textContent=(step+1)+' / 3 · '+['Walk-around','Equipment','Sign-off'][step];
    if(move) {save();inspection.scrollIntoView({block:'start'});}
  }
  next.addEventListener('click',()=>{
    const fields=parts.filter(p=>!p.hidden).flatMap(p=>[...p.querySelectorAll('input,textarea,select')]);
    if(fields.every(el=>el.reportValidity())){step++;show(true);}
  });
  back.addEventListener('click',()=>{if(step>0){step--;show(true);}});
  inspection.addEventListener('input',save);
  inspection.addEventListener('submit',event=>{if(step<2){event.preventDefault();next.click();}});
  show();
}
