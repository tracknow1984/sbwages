(() => {
  'use strict';
  const root = document.getElementById('blocktexx');
  if (!root) return;
  let model = JSON.parse(document.getElementById('bx-data').textContent);
  let state = 'QLD', revision = Number(root.dataset.revision), dirty = false, saving = false, generation = 0;
  const $ = id => document.getElementById(id);
  const fmt = (n, places = 1) => n == null ? 'Not set' : Number(n).toLocaleString('en-AU', {maximumFractionDigits: places});
  const money = n => n == null ? 'Not priced' : '$' + fmt(n, 0);
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const working = r => ['drive_min','service_min','depot_min','prep_min','wait_min'].some(k => r[k] == null) ? null : ['drive_min','service_min','depot_min','prep_min','wait_min'].reduce((a,k) => a + r[k], 0) / 60;
  function changed() { dirty = true; generation++; $('bx-save-status').textContent = 'Unsaved changes — select Save model.'; renderMetrics(); renderOverview(); }
  function summary(s) {
    const d = model.states[s]; let km = 0, work = 0, billed = 0, elapsed = 0, pending = 0, estimates = 0, active = 0;
    d.runs.forEach(r => {
      if (r.runs_4w == null) { pending++; return; }
      if (!r.runs_4w) return;
      active++;
      if (r.status !== 'verified') estimates++;
      const h = working(r);
      if (r.km == null || h == null || r.break_min == null) pending++;
      km += (r.km || 0) * r.runs_4w;
      if (h != null) { work += h * r.runs_4w; billed += Math.max(h,d.minimum_hours) * r.runs_4w; elapsed += (h + (r.break_min || 0)/60) * r.runs_4w; }
    });
    const assigned = new Set(d.runs.filter(r=>r.runs_4w!==0).flatMap(r=>r.site_ids));
    pending += model.sites.filter(s=>s.state===state&&!assigned.has(s.id)).length;
    let cost = null;
    if (active) {
      if (d.cost_mode === 'owned') cost = d.fixed_monthly;
      if (d.cost_mode === 'contractor' && d.hourly_rate != null) cost = billed * 13/12 * d.hourly_rate;
    }
    return {km,work,billed,elapsed,pending,estimates,cost,rate:!pending && d.monthly_kg && cost != null ? cost/d.monthly_kg : null, spare:d.available_weekly_hours*4-work};
  }
  function renderOverview() {
    $('bx-overview').replaceChildren(...Object.entries(model.states).map(([s,d]) => {
      const card=el('div'); card.append(el('span',s),el('strong',fmt(d.monthly_kg,0)+' kg'),el('small','Historical average / month'),el('p',d.depot || 'Depot not set')); return card;
    }));
  }
  function renderMetrics() {
    const v = summary(state);
    const items = [['Known km / 4 weeks',fmt(v.km,0)],['Known working hours / 4 weeks',fmt(v.work)],['Calendar-month working hours',fmt(v.work*13/12)],['Unallocated hours / 4 weeks',fmt(v.spare)],['Elapsed hours / 4 weeks',fmt(v.elapsed)],['Billable hours / 4 weeks',fmt(v.billed)],['Known collection cost / month',money(v.cost)],['Collection-only cost / kg',v.rate == null ? 'Incomplete' : '$'+fmt(v.rate,3)]];
    $('bx-metrics').replaceChildren(...items.map(([a,b]) => { const n=el('div',a); n.append(el('strong',b)); return n; }));
    $('bx-gaps').textContent = `${v.pending} run rows have missing frequency or measurements. ${v.estimates} active rows use estimates. Unallocated hours are not promised compaction capacity; allow for unplanned work and downstream loading. Cost excludes later stages and unentered costs. One contractor minimum per run row.`;
  }
  function inputField(key,label,options) {
    const d=model.states[state], wrap=el('label',label); let input;
    if (options) { input=el('select'); options.forEach(([v,t])=>{const o=el('option',t);o.value=v;input.append(o);}); }
    else { input=el('input'); input.type=key==='depot'?'text':'number'; if(input.type==='number'){input.min='0';input.step='any';} }
    input.value=d[key] == null ? '' : d[key]; input.setAttribute('aria-label',label);
    input.addEventListener('change',()=>{
      if (!input.checkValidity()) { input.reportValidity(); return; }
      d[key]=options || key==='depot' ? input.value : input.value===''?null:Number(input.value);
      if (key==='depot') { d.depot_status='assumed'; d.runs.forEach(r=>{r.status='unmeasured';r.km=null;r.drive_min=null;r.evidence='Depot changed — remeasure route.';}); render(); }
      changed();
    }); wrap.append(input); return wrap;
  }
  function renderSettings() {
    const d=model.states[state]; $('bx-state-title').textContent=state+' collection assumptions'; $('bx-depot-status').textContent='Depot: '+d.depot_status;
    $('bx-settings').replaceChildren(
      inputField('depot','Depot / starting location'),inputField('depot_status','Depot status',[['unconfirmed','Unconfirmed'],['assumed','Assumed'],['confirmed','Confirmed']]),
      inputField('monthly_kg','Average incoming kg / calendar month'),inputField('available_weekly_hours','Driver working hours / week'),
      inputField('cost_mode','Collection cost method',[['unpriced','Not priced yet'],['owned','Own truck + employee'],['contractor','Contractor']]),
      inputField('hourly_rate','Contractor $ / hour, ex GST'),inputField('minimum_hours','Minimum billed hours / attendance'),inputField('fixed_monthly','Owned collection cost $ / month, ex GST'));
    $('bx-state-notes').textContent=d.notes;
  }
  function renderRuns() {
    $('bx-runs').replaceChildren(); document.querySelectorAll('.bx-editor').forEach(n=>n.remove());
    model.states[state].runs.forEach(r=>{
      const tr=el('tr'), first=el('td'); first.append(el('strong',r.name),el('small',r.sequence),el('small',r.notes)); tr.append(first);
      ['runs_4w','km','drive_min','service_min','depot_min','prep_min','wait_min','break_min'].forEach(k=>{
        const td=el('td'),input=el('input'); input.type='number';input.min='0';input.step='any';input.value=r[k]??'';input.placeholder='TBC';input.setAttribute('aria-label',r.name+' '+k);
        input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}r[k]=input.value===''?null:Number(input.value);if(k==='km'||k==='drive_min'){r.status='estimated';badge.textContent='estimated';badge.className='bx-status';}hours.textContent=working(r)==null?'TBC':fmt(working(r),2);changed();});td.append(input);tr.append(td);
      });
      const hours=el('td',working(r)==null?'TBC':fmt(working(r),2));tr.append(hours);
      const status=el('td'),badge=el('span',r.status,'bx-status '+r.status);status.append(badge,el('small',r.evidence));tr.append(status);
      const actions=el('td',null,'no-print'),edit=el('button','Details','secondary');edit.type='button';edit.addEventListener('click',()=>editRun(r,tr));actions.append(edit);tr.append(actions);$('bx-runs').append(tr);
    });
  }
  function editRun(r,tr) {
    document.querySelectorAll('.bx-editor').forEach(n=>n.remove());
    const box=el('div',null,'bx-editor'),fields={};
    [['name','Run name'],['sequence','Sequence — include every depot return'],['notes','Capacity, access and other assumptions'],['evidence','Distance/time source and date']].forEach(([k,label])=>{const l=el('label',label),i=el(k==='name'?'input':'textarea');i.value=r[k];i.maxLength=2000;fields[k]=i;l.append(i);box.append(l);});
    const l=el('label','Measurement status'),status=el('select');['estimated','verified','unmeasured'].forEach(s=>{const o=el('option',s);o.value=s;status.append(o);});status.value=r.status;l.append(status);box.append(l);
    const sl=el('label','Linked customers (Ctrl/Cmd to select several)'),sites=el('select');sites.multiple=true;model.sites.filter(s=>s.state===state).forEach(s=>{const o=el('option',s.name+' — '+s.address);o.value=s.id;o.selected=r.site_ids.includes(s.id);sites.append(o);});sl.append(sites);box.append(sl);
    const actions=el('div',null,'actions'),apply=el('button','Apply details','primary'),cancel=el('button','Cancel','secondary'),remove=el('button','Remove run','secondary');[apply,cancel,remove].forEach(b=>b.type='button');
    apply.addEventListener('click',()=>{if(status.value==='verified'&&(!fields.evidence.value.trim()||r.km==null||r.drive_min==null)){alert('Enter distance, driving time and a measurement source/date before marking verified.');return;}if(fields.sequence.value!==r.sequence){r.km=null;r.drive_min=null;r.status='unmeasured';r.evidence='Sequence changed — remeasure route.';}else{r.status=status.value;r.evidence=fields.evidence.value;}['name','sequence','notes'].forEach(k=>r[k]=fields[k].value);r.site_ids=Array.from(sites.selectedOptions,o=>o.value);changed();renderRuns();renderSites();});
    cancel.addEventListener('click',()=>box.remove());remove.addEventListener('click',()=>{if(confirm('Remove this run from the draft?')){model.states[state].runs=model.states[state].runs.filter(x=>x.id!==r.id);changed();renderRuns();renderSites();}});actions.append(apply,cancel,remove);box.append(actions);tr.closest('.bx-scroll').after(box);box.scrollIntoView({block:'nearest'});
  }
  function renderSites() {
    const assigned=new Set(model.states[state].runs.filter(r=>r.runs_4w!==0).flatMap(r=>r.site_ids));
    $('bx-sites').replaceChildren(...model.sites.filter(s=>s.state===state).map(s=>{
      const n=el('details',null,'bx-site'),head=el('summary',s.name+' · '+s.frequency+(assigned.has(s.id)?'':' · Not linked to a run'));
      if(!assigned.has(s.id))head.className='bx-unassigned';n.append(head,el('p',s.address),el('p',s.equipment),el('p','Source rows: '+s.source_rows),el('p',s.notes));return n;
    }));
    const partners=model.partners.filter(s=>s.state===state);$('bx-partners').replaceChildren(...(partners.length?partners.map(p=>{const n=el('div',null,'bx-site');n.append(el('strong',p.name),el('p',p.address+' · '+p.frequency),el('p',p.notes));return n;}):[el('p','No decommissioning partner confirmed in the supplied source for this state.')]));
  }
  function render() { renderOverview();renderSettings();renderRuns();renderMetrics();renderSites();$('bx-source').textContent=model.source;$('bx-notes').textContent=model.notes;document.querySelectorAll('[data-state]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.state===state))); }
  document.querySelectorAll('[data-state]').forEach(b=>b.addEventListener('click',()=>{state=b.dataset.state;render();}));
  $('bx-add').addEventListener('click',()=>{model.states[state].runs.push({id:crypto.randomUUID(),name:'New collection day',sequence:'',notes:'',evidence:'',status:'unmeasured',site_ids:[],runs_4w:null,km:null,drive_min:null,service_min:0,depot_min:0,prep_min:15,wait_min:0,break_min:30});changed();renderRuns();});
  $('bx-save').addEventListener('click',async()=>{
    if(saving)return;saving=true;$('bx-save').disabled=true;const sentGeneration=generation;$('bx-save-status').textContent='Saving…';
    try {const response=await fetch('/admin/blocktexx',{method:'POST',headers:{'Accept':'application/json'},body:new URLSearchParams({csrf:root.dataset.csrf,revision:String(revision),model:JSON.stringify(model)})});if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Session expired or server unavailable. Download your draft before signing in again.');const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error||'Save failed.');revision=result.revision;dirty=generation!==sentGeneration;$('bx-save-status').textContent=dirty?'Earlier changes saved. New changes remain unsaved.':'Saved to SB Empire · '+new Date(result.saved).toLocaleString('en-AU');}
    catch(e){dirty=true;$('bx-save-status').textContent='Not saved: '+e.message;}
    finally{saving=false;$('bx-save').disabled=false;}
  });
  $('bx-download').addEventListener('click',()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(model,null,2)],{type:'application/json'})),a=el('a');a.href=u;a.download='BlockTexx-collection-draft.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);});
  $('bx-import').addEventListener('change',async e=>{
    const file=e.target.files[0];if(!file)return;
    try{if(!$('bx-replace').checked)throw new Error('Tick the replacement acknowledgement before importing.');if(file.size>900000)throw new Error('File exceeds 900 KB.');const response=await fetch('/admin/blocktexx/validate',{method:'POST',body:new URLSearchParams({csrf:root.dataset.csrf,model:await file.text()})});if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Session expired or validation unavailable. Existing draft retained.');const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error||'Invalid model file.');model=result.model;render();changed();$('bx-save-status').textContent='Imported draft — review the four states, then Save model.';}catch(err){$('bx-save-status').textContent=err.message;}finally{e.target.value='';$('bx-replace').checked=false;}
  });
  $('bx-print').addEventListener('click',()=>window.print());
  window.addEventListener('beforeunload',e=>{if(dirty||saving){e.preventDefault();e.returnValue='';}});
  render();
})();
