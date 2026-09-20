window.BlocktexxWeights=(()=>{
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const fmt=n=>n==null?'No sample':Number(n).toLocaleString('en-AU',{maximumFractionDigits:1});
  function sync(model){
    const totals=new Map();
    for(const r of model.weight_history?.pickups||[]){if(!r.site_id)continue;const x=totals.get(r.site_id)||{kg:0,count:0};x.kg+=r.kg;x.count++;totals.set(r.site_id,x);}
    model.sites.forEach(s=>{const x=totals.get(s.id);s.sample_pickup_kg=x?x.kg/x.count:null;s.sample_pickups=x?.count||0;s.sample_total_kg=x?.kg||0;});
  }
  function pickup(run,slot,sites=[]){
    if(run.activity_type&&run.activity_type!=='collection')return {kg:0,missing:0,source:'Transfer — no new intake'};
    if(slot.pickup_kg!=null)return {kg:slot.pickup_kg,missing:0,source:'Run scenario override'};
    let kg=0,missing=0,overridden=false;
    if(!run.site_ids?.length)return {kg:0,missing:1,source:'No pickup weight'};
    for(const id of run.site_ids){const site=sites.find(s=>s.id===id);const override=slot.pickup_by_site?.[id]??site?.scenario_pickup_kg;const value=override??site?.sample_pickup_kg;if(value==null)missing++;else kg+=value;if(override!=null)overridden=true;}
    return {kg,missing,source:overridden?'Scenario estimates':'Historical average estimates'};
  }
  function render(root,model,state,onChange,csrf,onImport){
    const open=[...root.querySelectorAll('details')].map(d=>d.open);
    root.replaceChildren(e('h2','Sample weights and break-even scenarios'));
    const records=model.weight_history?.pickups||[],total=records.reduce((n,r)=>n+r.kg,0),unmatched=records.filter(r=>!r.site_id);
    root.append(e('p',records.length+' historical pickups · '+fmt(total)+' kg imported · '+fmt(unmatched.reduce((n,r)=>n+r.kg,0))+' kg awaiting mapping.'));
    const upload=e('label','Import original sample workbook '),input=e('input');input.type='file';input.accept='.xlsx';input.setAttribute('aria-label','Import sample pickup weights');
    input.onchange=async()=>{if(!input.files[0])return;if(records.length&&!confirm('Replace the historical sample? Scenario overrides and calendar allocations will be retained.')){input.value='';return;}const snapshot=JSON.stringify(model);const form=new FormData();form.append('csrf',csrf);form.append('model',snapshot);form.append('file',input.files[0]);input.disabled=true;const status=e('p','Importing pickup dockets…');root.append(status);try{const response=await fetch('/admin/blocktexx/weights/import',{method:'POST',body:form});if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Sign in again before importing.');const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'Import failed');if(JSON.stringify(model)!==snapshot)throw Error('The draft changed during import. Your edits are retained; select the workbook again.');onImport(result.model);}catch(error){status.textContent=error.message;input.disabled=false;}};upload.append(input);root.append(upload);
    root.append(e('p','Historical material lines are combined by date, docket and customer. Calendar pickups use each mapped customer’s average kg per historical pickup. These are estimated future loads, not the actual dates in the sample. Changing pickup frequency changes projected volume at that average; it does not conserve the historical monthly volume.','bx-muted'));
    const rateLabel=e('label','Selling rate $ / kg (ex GST) '),rate=e('input');rate.type='number';rate.min=0;rate.max=10000;rate.step='any';rate.value=model.states[state].selling_per_kg??'';rate.placeholder='Enter rate to calculate break-even';rate.setAttribute('aria-label','Selling rate per kg');rate.onchange=()=>{if(!rate.checkValidity()){rate.reportValidity();return;}model.states[state].selling_per_kg=rate.value===''?null:Number(rate.value);onChange();};rateLabel.append(rate);root.append(rateLabel);
    root.append(e('p','Break-even kg = total costs ÷ selling rate. Volume changes hold entered day costs constant; revise hours, loads and costs if more volume needs more truck trips. Costs cover the current local model; interstate and processing remain separate.','bx-muted'));
    const details=e('details');details.open=open[0]||false;details.append(e('summary','Customer baseline and editable kg per pickup'));
    const table=e('table',null,'bx-resource-table'),head=e('tr');['Customer','Sample pickups','Sample average kg','Scenario kg / pickup'].forEach(t=>head.append(e('th',t)));table.append(head);
    model.sites.filter(s=>s.state===state).forEach(s=>{const row=e('tr');row.append(e('th',s.name),e('td',s.sample_pickups),e('td',fmt(s.sample_pickup_kg)));const cell=e('td'),value=e('input');value.type='number';value.min=0;value.max=1000000;value.step='any';value.value=s.scenario_pickup_kg??'';value.placeholder=s.sample_pickup_kg==null?'No sample':fmt(s.sample_pickup_kg);value.setAttribute('aria-label',s.name+' scenario kg per pickup');value.onchange=()=>{if(!value.checkValidity()){value.reportValidity();return;}s.scenario_pickup_kg=value.value===''?null:Number(value.value);onChange();};cell.append(value);row.append(cell);table.append(row);});const wrap=e('div',null,'bx-scroll');wrap.append(table);details.append(wrap);root.append(details);
    const audit=e('details');audit.open=open[1]||false;audit.append(e('summary','Reconcile all sample locations and unmatched weights'));
    const profiles=new Map();records.forEach(r=>{const p=profiles.get(r.profile)||{...r,kg:0,count:0};p.kg+=r.kg;p.count++;profiles.set(r.profile,p);});
    const auditTable=e('table',null,'bx-resource-table'),h=e('tr');['Sample location','Facility','Total kg','Pickups','Map to calendar customer'].forEach(t=>h.append(e('th',t)));auditTable.append(h);
    profiles.forEach(p=>{const row=e('tr');row.append(e('th',p.company+' · '+p.site),e('td',p.facility),e('td',fmt(p.kg)),e('td',p.count));const cell=e('td'),select=e('select');select.setAttribute('aria-label',p.company+' '+p.site+' '+p.facility+' mapping');const unset=e('option','Unmapped — excluded from calendar estimates');unset.value='';select.append(unset);model.sites.forEach(s=>{const o=e('option',s.state+' · '+s.name);o.value=s.id;select.append(o);});select.value=p.site_id;select.onchange=()=>{records.filter(r=>r.profile===p.profile).forEach(r=>r.site_id=select.value);onChange();};cell.append(select);row.append(cell);auditTable.append(row);});const aw=e('div',null,'bx-scroll');aw.append(auditTable);audit.append(aw);root.append(audit);
    if(unmatched.length)root.append(e('p','Unmatched locations are retained above and excluded from projected calendar kg. Resolve ambiguous locations or add the appropriate customer/run. All imported kilograms remain in the history.','bx-warning'));
    const unallocated=model.sites.filter(s=>s.state===state&&s.sample_pickups&&!model.states[state].runs.some(r=>r.site_ids.includes(s.id)&&window.createBlocktexxPlanner().slots(r).length));
    if(unallocated.length)root.append(e('p','Mapped customers without a calendar allocation: '+unallocated.map(s=>s.name).join(', '),'bx-warning'));
  }
  return {sync,pickup,render};
})();
