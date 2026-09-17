window.createBlocktexxRunView = function(getModel,getState,getPlans,onChange) {
  const host=document.getElementById('bx-run-view'),selection={},planner=window.createBlocktexxPlanner();
  const labels={cage:'cages',bin660:'660L bins',bin240:'240L bins',bin120:'120L bins',pallecon:'pallecons'};
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const fmt=n=>n==null?'To confirm':Number(n).toLocaleString('en-AU',{maximumFractionDigits:2});
  const contents=c=>Object.entries(labels).filter(([k])=>c?.[k]).map(([k,l])=>fmt(c[k])+' '+l).join(', ')||'Quantity to confirm';
  function render() {
    if(planner.isDragging())return;
    const model=getModel(),state=getState(),data=model.states[state],runs=data.runs;
    host.replaceChildren();
    planner.render(host,model,state,selection[state],id=>{selection[state]=id;render();},onChange,getPlans());
    const r=runs.find(r=>r.id===selection[state]);
    if(!r){host.append(e('p','Select a run above to view its collection details.'));return;}
    const root=e('section',null,'bx-selected-run');host.append(root);
    const close=e('button','Close run details','secondary');close.type='button';close.addEventListener('click',()=>{selection[state]=null;render();});root.append(close);
    if(r.activity_type && r.activity_type!=='collection'){
      root.append(e('h3',r.name),e('p',window.BlocktexxActivityLabels?.[r.activity_type]||r.activity_type),
        e('p',r.origin+' → '+r.destination),e('p','Cargo: '+(r.cargo||'To confirm')+' · '+fmt(r.movement_kg)+' kg moved (not additional intake)'),
        e('p',fmt(r.km)+' km · '+fmt(['drive_min','service_min','depot_min','prep_min','wait_min','break_min'].some(k=>r[k]==null)?null:['drive_min','service_min','depot_min','prep_min','wait_min','break_min'].reduce((n,k)=>n+r[k],0)/60)+' hours'),
        e('p','Included in local day hours and state costs. Verify this cargo fits the vehicle; collection switch-out planning does not apply.','bx-warning'));
      const edit=e('button','Edit movement','secondary');edit.type='button';edit.onclick=()=>document.dispatchEvent(new CustomEvent('bx-edit-movement',{detail:r.id}));root.append(edit);return;
    }
    const sites=r.site_ids.map(id=>model.sites.find(s=>s.id===id)).filter(Boolean);
    const p=getPlans()?.[state]?.[r.id];
    const allocated=planner.slots(r);
    const day=r.name.match(/\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b/gi);
    const week=r.name.match(/\bWeek\s+\d+\b/gi);
    const times=['drive_min','service_min','depot_min','prep_min','wait_min'];
    const work=times.some(k=>r[k]==null)?null:times.reduce((a,k)=>a+r[k],0);
    const totals={};Object.keys(labels).forEach(k=>totals[k]=sites.reduce((a,s)=>a+(s.containers?.[k]||0),0));
    const unknown=sites.filter(s=>!Object.values(s.containers||{}).some(Boolean)).length;
    const stops=p&&!p.issues.length?p.loads.reduce((a,l)=>a+l.stops.length,0):null;
    root.append(e('h3',r.name),e('p',state+' · Depot: '+(data.depot||'To confirm')),
      e('p',(allocated.length?'Allocated: '+allocated.map(s=>'Week '+s.week+' '+['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][s.day]).join(', ')+' · ':week?week.join(', ')+' · ':'')+(allocated.length?'':day?[...new Set(day)].join(', ')+' (from run name)':'Day of week: to confirm')+' · '+(r.runs_4w==null?'Frequency to confirm':r.runs_4w===0?'Paused':fmt(r.runs_4w)+' occurrences per four weeks')));
    const cards=e('div',null,'bx-metrics');
    [['Pickup locations',sites.length],['Estimated pickup stops / run',stops==null?(p?'To confirm':sites.length+' before load splits'):stops],['Distance / run',r.km==null?'To confirm':fmt(r.km)+' km'],['Elapsed time / run',work==null||r.break_min==null?'To confirm':fmt((work+r.break_min)/60)+' hours'],['Bins / run',fmt(totals.bin660+totals.bin240+totals.bin120)+(unknown?' + unknown':'')],['Cages / run',fmt(totals.cage)+(unknown?' + unknown':'')],['Depot loads',p?fmt(p.load_count):'To confirm'],['Measurement status',r.status]].forEach(([k,v])=>{const c=e('div',k);c.append(e('strong',v));cards.append(c);});
    root.append(cards,e('p','Quantities are per occurrence. Pickup stops include repeat visits when a collection spans multiple loads. Distance and time are whole-run allowances; estimated rows still need verification.'));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-customers'),head=e('tr');
    ['Pickup customer','Address','Switch-out per visit','Collection frequency'].forEach(t=>head.append(e('th',t)));table.append(head);
    sites.forEach(s=>{const row=e('tr');[s.name,s.address||'To confirm','Deliver '+contents(s.containers)+' empty → collect same full',s.frequency||'To confirm'].forEach(t=>row.append(e('td',t)));table.append(row);});
    wrap.append(table);root.append(e('h3','What is being picked up'),wrap);
    if(unknown)root.append(e('p',unknown+' customers have unconfirmed container quantities. Totals are incomplete.','bx-warning'));
    root.append(e('p','Empty containers required across this run: '+contents(totals)+'. Unload matching empties before collecting full containers. Confirm depot stock and swap handling times.'),e('h3','Route and depot returns'),e('p',r.sequence||'Sequence to confirm'));
    if(p){
      p.loads.forEach((l,i)=>{const card=e('div',null,'bx-load-card');card.append(e('strong','Load '+(i+1)+' · '+fmt(l.spaces)+' positions used · '+fmt(l.spare_spaces)+' spare'));
        card.append(e('p','Depart with EMPTY: '+contents(l.outbound_empty||{})));
        card.append(e('p',l.stops.map(s=>s.name+': '+contents(s.containers)).join(' → ')+' → depot'));card.append(e('p','Return with FULL: '+contents(l.return_full||{})));root.append(card);});
      if(p.extra_loads)root.append(e('p',fmt(p.extra_loads)+' extra loads need revised distance and time allowances.','bx-warning'));
      if(p.issues.length)root.append(e('p',p.issues.join('; '),'bx-warning'));
      root.append(e('p',p.payload_checked?'Configured loaded weights checked.':'Space-only estimate; loaded weights and usable payload need confirmation.'));
    }else root.append(e('p',['NSW','QLD'].includes(state)?'Truck loads are calculating or awaiting valid capacity inputs.':'Truck load planning has not been configured for this state.'));
    const timing=e('details');timing.append(e('summary','View time breakdown, evidence and notes'));
    [['Driving','drive_min'],['Customer handling','service_min'],['Depot handling','depot_min'],['Preparation','prep_min'],['Waiting','wait_min'],['Breaks','break_min']].forEach(([l,k])=>timing.append(e('p',l+': '+(r[k]==null?'To confirm':fmt(r[k])+' min'))));
    timing.append(e('p','Evidence: '+(r.evidence||'Not supplied')),e('p',r.notes||'No additional run notes.'));root.append(timing);
    const edit=e('button','Edit run in collection table','secondary');edit.type='button';edit.addEventListener('click',()=>{document.getElementById('bx-all-runs').open=true;const row=document.getElementById('bx-runs').children[runs.indexOf(r)];row?.scrollIntoView({block:'center',behavior:'smooth'});row?.querySelector('button')?.click();});root.append(edit);
  }
  return {render};
};
