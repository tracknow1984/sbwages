window.createBlocktexxCapacity = function(getModel, getState, onChange, csrf, onPlans) {
  const root=document.getElementById('bx-capacity'),kinds=['cage','bin660','bin240','bin120','pallecon'];
  const labels={cage:'Cages',bin660:'660L bins',bin240:'240L bins',bin120:'120L bins',pallecon:'Pallecons'};
  const e=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
  let requestId=0,timer,plans={};
  function number(label,value,set,step='any') { const l=e('label',label),i=e('input');i.type='number';i.min='0';i.step=step;i.value=value??'';i.setAttribute('aria-label',label);i.addEventListener('change',()=>{if(!i.checkValidity()){i.reportValidity();return;}set(i.value===''?null:Number(i.value));onChange();});l.append(i);return l; }
  function apply(run,plan) {
    if(plan.issues.length||!plan.loads.length)return;
    const parts=['Depot'];plan.loads.forEach(load=>{load.stops.forEach(s=>parts.push(s.name.replace('Blocktexx - ','')+' ['+kinds.filter(k=>s.containers[k]).map(k=>s.containers[k]+' '+labels[k]).join(', ')+']'));parts.push('depot');});
    run.original_sequence=run.original_sequence||run.sequence;run.sequence=parts.join(' → ');run.included_loads=plan.load_count;run.status='estimated';run.evidence='Capacity regrouped. Verify revised road distance, driving and unloading allowances.';
    if(plan.extra_loads){run.km=null;run.drive_min=null;run.depot_min=null;}
    onChange();
  }
  function render() {
    clearTimeout(timer);const token=++requestId,state=getState(),model=getModel();root.replaceChildren();plans={};
    if(!['NSW','QLD'].includes(state)){root.hidden=true;return;}root.hidden=false;
    const truck=model.states[state].truck;
    if(!truck){root.append(e('p','Load the saved model to initialise truck settings.'));return;}
    root.append(e('h2',state+' · Truck capacity and load splits'),e('p','Editable planning footprints, not measured fit. Default: 14 cages or 14 × 660L bins or 28 × 240L bins per load. Mixed loads share the same 14 positions. One-for-one bin exchanges assumed; no stacking/nesting credit. Verify container dimensions, usable payload and tailgate capacity.'));
    const settings=e('div');settings.className='bx-settings';settings.append(number('Truck pallet positions',truck.pallet_positions,v=>truck.pallet_positions=v),number('Usable payload kg (not GVM)',truck.payload_kg,v=>truck.payload_kg=v));
    kinds.forEach(k=>{settings.append(number(labels[k]+' positions each',truck.spaces[k],v=>truck.spaces[k]=v),number(labels[k]+' loaded kg each',truck.weights_kg[k],v=>truck.weights_kg[k]=v));});root.append(settings);
    const equipment=e('details');equipment.append(e('summary','Edit container quantities per customer collection'));
    const wrap=e('div');wrap.className='bx-scroll';const table=e('table');table.className='bx-customers';const h=e('tr');['Customer',...kinds.map(k=>labels[k])].forEach(x=>h.append(e('th',x)));table.append(h);
    model.sites.filter(s=>s.state===state).forEach(s=>{const row=e('tr');row.append(e('td',s.name));kinds.forEach(k=>{const td=e('td');td.append(number(s.name+' '+labels[k],s.containers[k],v=>s.containers[k]=v,'1'));row.append(td);});table.append(row);});wrap.append(table);equipment.append(wrap);root.append(equipment);
    const output=e('div');output.className='bx-capacity-output';output.textContent='Calculating load splits…';root.append(output);
    timer=setTimeout(async()=>{
      try {const response=await fetch('/admin/blocktexx/capacity',{method:'POST',body:new URLSearchParams({csrf,model:JSON.stringify(model)})});if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Sign in again to calculate capacity.');const result=await response.json();if(token!==requestId)return;if(!response.ok||!result.ok)throw Error(result.error||'Capacity calculation failed.');plans=result.plans;onPlans();output.replaceChildren();
        let monthlyBins=0,monthlyCages=0,monthlyLoads=0;
        model.states[state].runs.forEach(r=>{const p=plans[state][r.id];monthlyBins+=p.bin_count*(r.runs_4w||0);monthlyCages+=p.containers.cage*(r.runs_4w||0);monthlyLoads+=p.load_count*(r.runs_4w||0);
          const detail=e('details');detail.className='bx-site';detail.append(e('summary',`${r.name}: ${p.bin_count} bins + ${p.containers.cage} cages + ${p.containers.pallecon} pallecons · ${p.load_count} loads`));
          detail.append(e('p',p.payload_checked?'Configured payload checked; geometry and tailgate still need verification.':'Space-only plan: loaded weights / usable payload not fully confirmed.'));
          p.loads.forEach((load,index)=>{const description=load.stops.map(s=>s.name.replace('Blocktexx - ','')+': '+kinds.filter(k=>s.containers[k]).map(k=>s.containers[k]+' '+labels[k]).join(', ')).join(' → ');detail.append(e('p',`Load ${index+1}: depot → ${description} → depot. ${load.spaces}/ ${truck.pallet_positions} positions; ${load.spare_spaces} spare.`));});
          if(p.issues.length)detail.append(e('p',p.issues.join('; ')));
          if(p.extra_loads){const warning=e('p',`${p.extra_loads} additional loads beyond the current time/km allowance. Apply split, then enter revised driving and depot handling times.`);warning.className='bx-frequency-gap';detail.append(warning);}
          const count=number(r.name+' loads included in current km/time',r.included_loads,v=>r.included_loads=v,'1');detail.append(count);
          const button=e('button','Apply split to this run');button.type='button';button.className='secondary';button.disabled=!!p.issues.length||!p.loads.length;button.addEventListener('click',()=>apply(r,p));detail.append(button);output.append(detail);
        });
        output.prepend(e('p',`Known four-week service demand: ${monthlyBins.toLocaleString('en-AU')} bin services, ${monthlyCages.toLocaleString('en-AU')} cage services, ${monthlyLoads.toLocaleString('en-AU')} depot loads. Ad hoc/unconfirmed frequencies excluded. These are services, not unique equipment.`));
      } catch(error){if(token===requestId)output.textContent=error.message;}
    },250);
  }
  return {render, getPlans:()=>plans};
};
