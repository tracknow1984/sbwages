(() => {
  'use strict';
  const root = document.getElementById('blocktexx');
  if (!root) return;
  let model = JSON.parse(document.getElementById('bx-data').textContent);
  let state = 'QLD', revision = Number(root.dataset.revision), dirty = false, saving = false, generation = 0;
  let capacityUI, runView, consolidation;
  let activePane = 'planner';
  function renderResources() { window.renderBlocktexxResources?.(model,state,()=>{changed();renderSites();}); }
  function renderPane() { $('bx-planner-pane').hidden=activePane!=='planner';$('bx-resources').hidden=activePane!=='resources';document.querySelectorAll('[data-bx-pane]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.bxPane===activePane))); }
  const $ = id => document.getElementById(id);
  const fmt = (n, places = 1) => n == null ? 'Not set' : Number(n).toLocaleString('en-AU', {maximumFractionDigits: places});
  const money = n => n == null ? 'Not priced' : '$' + fmt(n, 0);
  const frequencies = [['Weekly',4],['Twice weekly',8],['Three times weekly',12],['Weekdays',20],['Fortnightly',2],['Every 4 weeks',1],['Every 8 weeks',.5],['Paused',0],['Ad hoc / unconfirmed',null],['Custom','custom']];
  const visits = s => s.visits_4w ?? null;
  const plannedVisits = s => model.states[s.state].runs.filter(r=>r.site_ids.includes(s.id)).reduce((a,r)=>a+(r.runs_4w||0),0);
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const working = r => ['drive_min','service_min','depot_min','prep_min','wait_min'].some(k => r[k] == null) ? null : ['drive_min','service_min','depot_min','prep_min','wait_min'].reduce((a,k) => a + r[k], 0) / 60;
  function saveStatus(message) {
    $('bx-save-status').textContent=message;
    $('bx-resources-save-status').textContent=message;
    $('bx-cost-save-status').textContent=message;
  }
  function changed() { dirty = true; generation++; saveStatus('Unsaved changes — select Save model, Save resources or Save cost model.'); capacityUI?.render(); renderMetrics(); renderOverview(); runView?.render(); renderResources();consolidation?.render(); }
  function summary(s) {
    const d = model.states[s]; let km = 0, work = 0, billed = 0, elapsed = 0, pending = 0, estimates = 0, active = 0;
    d.runs.forEach(r => {
      if (r.runs_4w == null) { pending++; return; }
      if (!r.runs_4w) return;
      active++;
      if (r.status !== 'verified') estimates++;
      const plan = capacityUI?.getPlans()?.[s]?.[r.id];
      if (['NSW','QLD'].includes(s) && (!plan || plan.issues.length || plan.extra_loads)) pending++;
      const h = working(r);
      if (r.km == null || h == null || r.break_min == null) pending++;
      km += (r.km || 0) * r.runs_4w;
      if (h != null) { work += h * r.runs_4w; billed += Math.max(h,d.minimum_hours) * r.runs_4w; elapsed += (h + (r.break_min || 0)/60) * r.runs_4w; }
    });
    const assigned = new Set(d.runs.filter(r=>r.runs_4w!==0).flatMap(r=>r.site_ids));
    pending += model.sites.filter(site=>site.state===s&&!assigned.has(site.id)).length;
    pending += model.sites.filter(site=>site.state===s&&visits(site)!=null&&Math.abs(visits(site)-plannedVisits(site))>.001).length;
    let cost = null;
    if (active) {
      if (d.cost_mode === 'owned') cost = d.fixed_monthly;
      if (d.cost_mode === 'contractor' && d.hourly_rate != null) cost = billed * 13/12 * d.hourly_rate;
    }
    if(d.cost_profile?.enabled&&window.BlocktexxCosts){
      const comparison=window.BlocktexxCosts.calculate(d);cost=comparison.selected;billed=comparison.billed_hours_month*12/13;
      if(!comparison.schedule_complete)pending++;
    }
    return {km,work,billed,elapsed,pending,estimates,cost,rate:!pending && d.monthly_kg && cost != null ? cost/d.monthly_kg : null, spare:d.available_weekly_hours*4-work};
  }
  function renderOverview() {
    $('bx-overview').replaceChildren(...Object.entries(model.states).map(([s,d]) => {
      const customers=model.sites.filter(site=>site.state===s),total=customers.reduce((a,site)=>a+(visits(site)||0),0),unknown=customers.filter(site=>visits(site)==null).length;
      const card=el('div'); card.append(el('span',s),el('strong',fmt(d.monthly_kg,0)+' kg'),el('small','Historical average / month'),el('p',d.depot || 'Depot not set'),el('small',`${customers.length} customers · ${fmt(total,1)} planned visits / 4 weeks · ${fmt(total*13/12,1)} / month${unknown?' · '+unknown+' unconfirmed':''}`)); return card;
    }));
  }
  function renderMetrics() {
    const v = summary(state);
    window.BlocktexxCosts?.renderComparison($('bx-cost-comparison'),model.states[state],v.pending);
    const items = [['Known km / 4 weeks',fmt(v.km,0)],['Known working hours / 4 weeks',fmt(v.work)],['Calendar-month working hours',fmt(v.work*13/12)],['Unallocated hours / 4 weeks',fmt(v.spare)],['Elapsed hours / 4 weeks',fmt(v.elapsed)],['Billable hours / 4 weeks',fmt(v.billed)],['Known collection cost / month',money(v.cost)],['Collection-only cost / kg',v.rate == null ? 'Incomplete' : '$'+fmt(v.rate,3)]];
    $('bx-metrics').replaceChildren(...items.map(([a,b]) => { const n=el('div',a); n.append(el('strong',b)); return n; }));
    $('bx-gaps').textContent = `${v.pending} gaps: missing frequency/measurements, unresolved truck capacity, unassigned customers or customer frequencies that differ from the route plan. ${v.estimates} active rows use estimates. Customer frequency edits update visit demand immediately; revise affected grouped runs to update kilometres, hours and costs. Historic kilograms do not change automatically. Unallocated hours still need to cover downstream work.`;
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
    const d=model.states[state]; $('bx-state-title').textContent=state+' cost modelling'; $('bx-depot-status').textContent='Depot: '+d.depot_status;
    $('bx-settings').replaceChildren(
      inputField('depot','Depot / starting location'),inputField('depot_status','Depot status',[['unconfirmed','Unconfirmed'],['assumed','Assumed'],['confirmed','Confirmed']]),
      inputField('monthly_kg','Average incoming kg / calendar month'),inputField('available_weekly_hours','Driver working hours / week'),
      inputField('cost_mode','Collection cost method',[['unpriced','Not priced yet'],['owned','Own truck + employee'],['contractor','Contractor']]));
    $('bx-legacy-costs').replaceChildren(
      inputField('hourly_rate','Legacy contractor $ / hour (profile disabled)'),inputField('minimum_hours','Legacy minimum hours (profile disabled)'),inputField('fixed_monthly','Legacy company $ / month (profile disabled)'));
    window.BlocktexxCosts?.renderInputs($('bx-cost-inputs'),d,changed);
    $('bx-state-notes').textContent=d.notes;
  }
  function renderRuns() {
    runView?.render();
    $('bx-runs').replaceChildren(); document.querySelectorAll('.bx-editor').forEach(n=>n.remove());
    model.states[state].runs.forEach(r=>{
      const tr=el('tr'), first=el('td'); first.append(el('strong',r.name),el('small',r.sequence),el('small',r.notes)); tr.append(first);
      ['runs_4w','km','drive_min','service_min','depot_min','prep_min','wait_min','break_min'].forEach(k=>{
        const td=el('td'),input=el('input'); input.type='number';input.min='0';input.step='any';input.value=r[k]??'';input.placeholder='TBC';input.setAttribute('aria-label',r.name+' '+k);
        input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}r[k]=input.value===''?null:Number(input.value);if(k==='km'||k==='drive_min'){r.status='estimated';badge.textContent='estimated';badge.className='bx-status';}hours.textContent=working(r)==null?'TBC':fmt(working(r),2);changed();if(k==='runs_4w')renderSites();});td.append(input);tr.append(td);
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
    const table=el('table',null,'bx-customers'),thead=el('thead'),head=el('tr');
    ['Customer / address','Spreadsheet frequency','Model frequency','Visits / 4 weeks','Visits / month','Route plan / 4 weeks'].forEach(t=>head.append(el('th',t)));thead.append(head);table.append(thead);const body=el('tbody');
    model.sites.filter(s=>s.state===state).forEach(s=>{
      const row=el('tr'),who=el('td'),details=el('details'),title=el('summary',s.name);details.append(title,el('p',s.equipment),el('p','Source rows: '+s.source_rows),el('p',s.notes));who.append(details,el('small',s.address));row.append(who,el('td',s.source_frequency||s.frequency));
      const choice=el('select'),td=el('td');choice.setAttribute('aria-label',s.name+' frequency');frequencies.forEach(([label,value])=>{const o=el('option',label);o.value=String(value);choice.append(o);});
      choice.value=frequencies.some(([,n])=>n===visits(s))?String(visits(s)):'custom';td.append(choice);row.append(td);
      const input=el('input'),count=el('td');input.type='number';input.min='0';input.max='124';input.step='any';input.value=visits(s)??'';input.placeholder='TBC';input.setAttribute('aria-label',s.name+' visits per four weeks');count.append(input);row.append(count,el('td',visits(s)==null?'TBC':fmt(visits(s)*13/12,2)));
      const planned=plannedVisits(s),mismatch=visits(s)!=null&&Math.abs(planned-visits(s))>.001,last=el('td',fmt(planned,2)+(mismatch?' — update runs':!assigned.has(s.id)?' — unassigned':''));if(mismatch)last.className='bx-frequency-gap';row.append(last);
      choice.addEventListener('change',()=>{if(choice.value==='custom'){input.focus();return;}s.visits_4w=choice.value==='null'?null:Number(choice.value);s.frequency=choice.selectedOptions[0].textContent;changed();renderSites();});
      input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}s.visits_4w=input.value===''?null:Number(input.value);s.frequency=s.visits_4w==null?'Ad hoc / unconfirmed':`Custom: ${s.visits_4w} visits / 4 weeks`;changed();renderSites();});body.append(row);
    });table.append(body);$('bx-sites').replaceChildren(table);
    const partners=model.partners.filter(s=>s.state===state);$('bx-partners').replaceChildren(...(partners.length?partners.map(p=>{const n=el('div',null,'bx-site');n.append(el('strong',p.name),el('p',p.address+' · '+p.frequency),el('p',p.notes));return n;}):[el('p','No decommissioning partner confirmed in the supplied source for this state.')]));
  }
  function render() { consolidation?.render();renderResources();renderPane();renderOverview();renderSettings();renderRuns();renderMetrics();renderSites();capacityUI?.render();$('bx-source').textContent=model.source;$('bx-notes').textContent=model.notes;document.querySelectorAll('[data-state]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.state===state))); }
  document.querySelectorAll('[data-state]').forEach(b=>b.addEventListener('click',()=>{state=b.dataset.state;render();}));
  document.querySelectorAll('[data-bx-pane]').forEach(b=>b.addEventListener('click',()=>{activePane=b.dataset.bxPane;renderResources();renderPane();}));
  $('bx-add').addEventListener('click',()=>{model.states[state].runs.push({id:crypto.randomUUID(),name:'New collection day',sequence:'',notes:'',evidence:'',status:'unmeasured',site_ids:[],runs_4w:null,km:null,drive_min:null,service_min:0,depot_min:0,prep_min:15,wait_min:0,break_min:30});changed();renderRuns();});
  async function saveModel() {
    if(saving)return;
    const invalid=$('bx-resource-content').querySelector('input:invalid')||$('bx-cost-inputs').querySelector('input:invalid');
    if(invalid){activePane=invalid.closest('#bx-resources')?'resources':'planner';renderPane();let parent=invalid.parentElement;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}invalid.reportValidity();saveStatus('Not saved: correct the highlighted value.');return;}
    saving=true;
    [$('bx-save'),$('bx-save-resources'),$('bx-save-costs')].forEach(b=>b.disabled=true);
    const sentGeneration=generation;saveStatus('Saving to database…');
    try {
      const response=await fetch('/admin/blocktexx',{method:'POST',headers:{'Accept':'application/json'},body:new URLSearchParams({csrf:root.dataset.csrf,revision:String(revision),model:JSON.stringify(model)})});
      if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Session expired or server unavailable. Download your draft before signing in again.');
      const result=await response.json();
      if(!response.ok||!result.ok)throw new Error(result.error||'Save failed.');
      revision=result.revision;dirty=generation!==sentGeneration;
      saveStatus(dirty?'Earlier changes saved. New changes remain unsaved.':'Saved to database · '+new Date(result.saved).toLocaleString('en-AU'));
    } catch(e){dirty=true;saveStatus('Not saved: '+e.message);}
    finally{saving=false;[$('bx-save'),$('bx-save-resources'),$('bx-save-costs')].forEach(b=>b.disabled=false);}
  }
  $('bx-save').addEventListener('click',saveModel);
  $('bx-save-resources').addEventListener('click',saveModel);
  $('bx-save-costs').addEventListener('click',saveModel);
  $('bx-download').addEventListener('click',()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(model,null,2)],{type:'application/json'})),a=el('a');a.href=u;a.download='BlockTexx-collection-draft.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);});
  $('bx-import').addEventListener('change',async e=>{
    const file=e.target.files[0];if(!file)return;
    try{if(!$('bx-replace').checked)throw new Error('Tick the replacement acknowledgement before importing.');if(file.size>900000)throw new Error('File exceeds 900 KB.');const response=await fetch('/admin/blocktexx/validate',{method:'POST',body:new URLSearchParams({csrf:root.dataset.csrf,model:await file.text()})});if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Session expired or validation unavailable. Existing draft retained.');const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error||'Invalid model file.');model=result.model;render();changed();saveStatus('Imported draft — review the four states, then Save model.');}catch(err){saveStatus(err.message);}finally{e.target.value='';$('bx-replace').checked=false;}
  });
  $('bx-print').addEventListener('click',()=>window.print());
  window.addEventListener('beforeunload',e=>{if(dirty||saving){e.preventDefault();e.returnValue='';}});
  capacityUI=window.createBlocktexxCapacity?.(()=>model,()=>state,()=>{changed();renderRuns();renderSites();},root.dataset.csrf,()=>{renderMetrics();runView?.render();});
  runView=window.createBlocktexxRunView(()=>model,()=>state,()=>capacityUI?.getPlans(),()=>{changed();renderRuns();renderSites();});
  consolidation=window.createBlocktexxConsolidation?.(()=>model,()=>state,()=>{changed();renderRuns();renderSites();},root.dataset.csrf);
  render();
})();
