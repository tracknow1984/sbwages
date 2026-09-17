window.createBlocktexxConsolidation = function(getModel,getState,onChange,csrf) {
  const root=document.getElementById('bx-consolidation'),days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const e=(t,v,c)=>{const n=document.createElement(t);if(v!=null)n.textContent=v;if(c)n.className=c;return n;};
  const schedule=window.createBlocktexxPlanner();
  let generation=0;
  function candidates(model,state) {
    const runs=model.states[state].runs,sites=new Map(model.sites.map(s=>[s.id,s])),result=[];
    for(let i=0;i<runs.length;i++)for(let j=i+1;j<runs.length;j++){
      const a=runs[i],b=runs[j],sa=schedule.slots(a),sb=schedule.slots(b);
      if(!a.runs_4w||a.runs_4w!==b.runs_4w||!Number.isInteger(a.runs_4w)||!a.site_ids.length||!b.site_ids.length)continue;
      if(a.site_ids.some(id=>b.site_ids.includes(id)))continue;
      const weeks=sa.map(s=>s.week).sort().join(),other=sb.map(s=>s.week).sort().join();
      if(!weeks||weeks!==other||sa.length!==a.runs_4w||sb.length!==b.runs_4w||new Set(sa.map(s=>s.week)).size!==sa.length)continue;
      const customers=[...a.site_ids,...b.site_ids].map(id=>sites.get(id));
      if(customers.some(s=>!s||s.day_rule==='unknown'||!s.day_rule))continue;
      const areas=new Set(customers.map(s=>(s.service_area||'').trim().toLowerCase()));
      if(areas.size!==1||areas.has(''))continue;
      const allowed=days.map((_,d)=>d).filter(d=>customers.every(s=>{
        if(s.day_rule==='fixed'){
          const original=a.site_ids.includes(s.id)?sa:sb;
          if(!s.service_days?.includes(d)||original.some(slot=>slot.day!==d))return false;
        }
        return !runs.some(other=>other!==a&&other!==b&&other.site_ids.includes(s.id)&&schedule.slots(other).some(slot=>sa.some(w=>w.week===slot.week)&&slot.day===d));
      }));
      if(!allowed.length)continue;
      result.push({a,b,customers,area:customers[0].service_area,allowed,weeks:sa.map(s=>s.week)});
    }
    return result;
  }
  function render() {
    const token=++generation,model=getModel(),state=getState(),wasOpen=root.querySelector('details')?.open;
    root.replaceChildren(e('h2',state+' · Consolidate nearby collections'),
      e('p','Confirm customer days and collection areas first. Suggestions combine separate runs in the same area and cycle weeks. Fixed days are protected; unconfirmed customers are excluded. This is an area-based shortlist, not a verified shortest-road-route calculation.'));
    const settings=e('details');settings.open=!!wasOpen;settings.append(e('summary','Customer day requirements and collection areas'));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table'),header=e('tr');
    ['Customer','Area / proximity group','Day requirement','Customer days'].forEach(t=>header.append(e('th',t)));table.append(header);
    model.sites.filter(s=>s.state===state).forEach(s=>{
      const row=e('tr');row.append(e('th',s.name));const area=e('input');area.type='text';area.value=s.service_area||'';area.maxLength=100;area.setAttribute('aria-label',s.name+' collection area');
      area.addEventListener('change',()=>{s.service_area=area.value.trim();onChange();});const at=e('td');at.append(area);row.append(at);
      const rule=e('select');[['unknown','Day not confirmed'],['flexible','Flexible'],['fixed','Customer-set days']].forEach(([v,l])=>{const o=e('option',l);o.value=v;rule.append(o);});rule.value=s.day_rule||'unknown';rule.setAttribute('aria-label',s.name+' day requirement');
      rule.addEventListener('change',()=>{if(rule.value==='fixed'&&!s.service_days?.length){rule.value=s.day_rule||'unknown';alert('Select the customer’s required days first, then choose Customer-set days.');return;}s.day_rule=rule.value;onChange();});const rt=e('td');rt.append(rule);row.append(rt);
      const dt=e('td');days.forEach((day,index)=>{const label=e('label'),c=e('input');c.type='checkbox';c.checked=s.service_days?.includes(index)||false;c.setAttribute('aria-label',s.name+' '+day);c.addEventListener('change',()=>{const selected=new Set(s.service_days||[]);c.checked?selected.add(index):selected.delete(index);if(s.day_rule==='fixed'&&!selected.size){c.checked=true;alert('A fixed-day customer needs at least one day.');return;}s.service_days=[...selected];onChange();});label.append(c,document.createTextNode(day.slice(0,3)+' '));dt.append(label);});row.append(dt);table.append(row);
    });wrap.append(table);settings.append(wrap);root.append(settings);
    const possible=candidates(model,state);
    root.append(e('p',possible.length+' possible grouped runs. Confirm geographical suitability and remeasure driving distance/time before using a proposal rate.'));
    if(!possible.length)root.append(e('p','No compatible pairs yet. Confirm flexible/fixed days and use the same area name for nearby customers. Runs must cover matching cycle weeks and have no repeated customers.'));
    possible.slice(0,30).forEach(c=>{
      const card=e('div',null,'bx-load-card');card.append(e('h3',c.area),e('p',c.a.name+' + '+c.b.name),e('p',c.customers.length+' customer locations · '+c.weeks.length+' grouped days per four weeks'));
      const label=e('label','Consolidated day '),day=e('select');c.allowed.forEach(d=>{const o=e('option',days[d]);o.value=d;day.append(o);});
      const current=schedule.slots(c.a)[0]?.day;if(c.allowed.includes(current))day.value=current;label.append(day);card.append(label);
      const preview=e('button','Preview combined run','secondary');preview.type='button';const output=e('div');card.append(preview,output);root.append(card);
      day.addEventListener('change',()=>output.replaceChildren());
      preview.addEventListener('click',async()=>{
        const selectedDay=Number(day.value),draft=JSON.parse(JSON.stringify(model)),d=draft.states[state];
        const r={...c.a,id:'group-'+crypto.randomUUID(),name:'Grouped '+c.area+' — '+days[selectedDay],site_ids:[...c.a.site_ids,...c.b.site_ids],planner_slots:c.weeks.map(week=>({week,day:selectedDay})),sequence:'Depot → '+c.customers.map(s=>s.name).join(' → ')+' → depot',original_sequence:c.a.sequence+' | '+c.b.sequence,km:null,drive_min:null,depot_min:null,status:'unmeasured',evidence:'Grouped by area and confirmed customer day rules. Measure the full route and depot returns before pricing.',notes:'Replaces: '+c.a.name+'; '+c.b.name,included_loads:1};
        for(const key of ['service_min','wait_min'])r[key]=c.a[key]==null||c.b[key]==null?null:c.a[key]+c.b[key];
        for(const key of ['prep_min','break_min'])r[key]=c.a[key]==null||c.b[key]==null?null:Math.max(c.a[key],c.b[key]);
        d.runs=d.runs.filter(x=>x.id!==c.a.id&&x.id!==c.b.id);d.runs.push(r);
        output.textContent='Checking grouped capacity…';preview.disabled=true;
        try{
          const response=await fetch('/admin/blocktexx/capacity',{method:'POST',body:new URLSearchParams({csrf,model:JSON.stringify(draft)})});
          const result=await response.json();if(token!==generation||Number(day.value)!==selectedDay)return;
          if(!response.ok||!result.ok)throw Error(result.error||'Unable to preview grouped run.');
          const p=result.plans?.[state]?.[r.id];output.replaceChildren();
          if(p?.issues.length){output.append(e('p',p.issues.join('; ')));return;}
          if(p){
            r.included_loads=p.load_count||1;
            const parts=['Depot'];p.loads.forEach(load=>{load.stops.forEach(s=>parts.push(s.name));parts.push('depot');});r.sequence=parts.join(' → ');
            output.append(e('p',p.container_count+' empty switch-outs / full returns · '+p.load_count+' truckloads. Depot returns are required between loads.'));
          }else output.append(e('p','Truck capacity must be confirmed for this state.'));
          output.append(e('p','One grouped attendance replaces two for each selected week. Kilometres, driving and depot handling will be marked incomplete until remeasured. Savings are not yet quantified.'));
          const apply=e('button','Use grouped run in draft','primary');apply.type='button';
          apply.addEventListener('click',()=>{if(token!==generation)return;model.states[state].runs=d.runs;onChange();});output.append(apply);
        }catch(error){if(token===generation)output.textContent=error.message;}
        finally{preview.disabled=false;}
      });
    });
  }
  return {render,candidates};
};
