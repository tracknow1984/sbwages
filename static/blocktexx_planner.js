window.createBlocktexxPlanner = function() {
  const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const categories=['Weekly','Fortnightly','Monthly','Ad hoc'];
  const settings={};
  let popup=null, dismissPopup=null, allocationPopup=null;
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const fmt=n=>n==null?'TBC':Number(n).toLocaleString('en-AU',{maximumFractionDigits:1});
  function slots(r) {
    if(Array.isArray(r.planner_slots))return r.planner_slots;
    const d=days.findIndex(day=>new RegExp('\\b'+day+'\\b','i').test(r.name));
    if(d<0||!r.runs_4w)return [];
    const w=r.name.match(/\bWeek\s+([1-4])\b/i);
    if(w&&r.runs_4w===1)return [{week:Number(w[1]),day:d}];
    if(!w&&r.runs_4w===4)return [1,2,3,4].map(week=>({week,day:d}));
    return [];
  }
  function groups(r,sites) {
    if(r.planner_frequency)return [r.planner_frequency];
    if(r.runs_4w==null||r.runs_4w===0)return ['Ad hoc'];
    const values=sites.filter(s=>r.site_ids.includes(s.id)).map(s=>s.visits_4w);
    const result=new Set(values.map(v=>v==null?'Ad hoc':v>=4?'Weekly':v>=2?'Fortnightly':'Monthly'));
    if(!result.size)result.add(r.runs_4w>=4?'Weekly':r.runs_4w>=2?'Fortnightly':'Monthly');
    return [...result];
  }
  function render(root,model,state,selected,onSelect,onChange,plans) {
    const config=settings[state]||(settings[state]={category:'Weekly',span:4,start:1,all:true});
    const runs=model.states[state].runs,sites=model.sites;
    const refresh=()=>onSelect(selected);
    root.append(e('h2','Collection planner'));
    const bar=e('div',null,'bx-planner-controls');
    const frequencyLabel=e('label','Frequency'),frequency=e('select');frequency.id='bx-frequency-select';frequency.setAttribute('aria-label','Collection frequency');
    ['All frequencies',...categories].forEach(c=>{const o=e('option',c);o.value=c;frequency.append(o);});
    frequency.value=config.all?'All frequencies':config.category;
    frequency.addEventListener('change',()=>{config.all=frequency.value==='All frequencies';if(!config.all)config.category=frequency.value;onSelect(null);});
    frequencyLabel.append(frequency);bar.append(frequencyLabel);
    const view=e('label','Planner view'),select=e('select');select.setAttribute('aria-label','Planner view');
    [[1,'Week'],[2,'Fortnight'],[4,'Month (4-week cycle)']].forEach(([v,t])=>{const o=e('option',t);o.value=v;select.append(o);});select.value=config.span;
    select.addEventListener('change',()=>{config.span=Number(select.value);config.start=1;refresh();});view.append(select);bar.append(view);
    if(config.span<4){const label=e('label','Cycle period'),period=e('select');period.setAttribute('aria-label','Cycle period');
      (config.span===1?[1,2,3,4]:[1,3]).forEach(w=>{const o=e('option',config.span===1?'Week '+w:'Weeks '+w+'–'+(w+1));o.value=w;period.append(o);});
      period.value=config.start;period.addEventListener('change',()=>{config.start=Number(period.value);refresh();});label.append(period);bar.append(label);}
    root.append(bar);
    const legend=e('div',null,'bx-frequency-legend');legend.setAttribute('aria-label','Frequency colours');
    categories.forEach(c=>{const badge=e('span',c,'bx-frequency-label');badge.dataset.frequency=c;legend.append(badge);});root.append(legend);
    root.append(e('p','Click VIEW DAY for a popup summary, or select a run card for its details. Mixed-frequency runs appear in each relevant category. Monthly is a four-week planning cycle; eight-weekly work is labelled separately.','bx-muted'));
    const visible=runs.filter(r=>config.all||groups(r,sites).includes(config.category));
    function card(r,week,day) {
      const b=e('button',null,'bx-planner-run');b.type='button';b.dataset.runId=r.id;b.dataset.frequency=(week==null||config.all)?groups(r,sites)[0]:config.category;b.setAttribute('aria-pressed',String(r.id===selected));
      const name=r.name.replace(/^Week\s+\d+\s+/i,'').replace(/^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Weekly|Fortnightly)\s*—?\s*/i,'');
      const minutes=['drive_min','service_min','depot_min','prep_min','wait_min','break_min'];
      const hours=minutes.some(k=>r[k]==null)?null:minutes.reduce((n,k)=>n+r[k],0)/60;
      b.append(e('strong',name),e('small',r.site_ids.length+' locations · '+fmt(r.km)+' km · '+fmt(hours)+' h'));
      if(r.runs_4w===.5)b.append(e('small','Every 8 weeks — allocate only when due'));
      if(r.runs_4w===0)b.append(e('small','Paused'));
      if(week!=null&&r.runs_4w!=null&&slots(r).length!==r.runs_4w)b.append(e('small','Review frequency: '+slots(r).length+' allocated / '+fmt(r.runs_4w)+' planned'));
      const badges=e('div',null,'bx-frequency-badges');
      groups(r,sites).forEach(c=>{const badge=e('span',c,'bx-frequency-label');badge.dataset.frequency=c;badges.append(badge);});b.append(badges);
      b.addEventListener('click',event=>{event.stopPropagation();if(week!=null){config.selectedDay={week,day};config.dayOpen=true;}if(week==null){config.allocateId=r.id;config.allocationDraft=null;config.dayOpen=false;}onSelect(r.id);});return b;
    }
    const hiddenAllocated=runs.filter(r=>slots(r).length&&!visible.includes(r)).length;
    if(hiddenAllocated)root.append(e('p',hiddenAllocated+' allocated runs hidden by the frequency filter. Choose All frequencies to see them.','bx-warning'));
    const assigned=visible.filter(r=>slots(r).length),hasWeekend=assigned.some(r=>slots(r).some(s=>s.day>4));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-planner-grid'),head=e('tr');
    head.append(e('th','Week'));days.slice(0,hasWeekend?7:5).forEach(day=>head.append(e('th',day)));table.append(head);
    for(let week=config.start;week<config.start+config.span;week++){
      const row=e('tr');row.append(e('th','Week '+week));
      days.slice(0,hasWeekend?7:5).forEach((day,d)=>{
        const cell=e('td');cell.dataset.week=week;cell.dataset.day=d;
        const active=config.selectedDay?.week===week&&config.selectedDay?.day===d;
        cell.classList.toggle('bx-day-selected',active);
        const choose=()=>{config.selectedDay={week,day:d};config.dayOpen=true;onSelect(null);};
        cell.addEventListener('click',choose);
        const dayButton=e('button','VIEW DAY','bx-view-day');dayButton.type='button';
        dayButton.setAttribute('aria-label','View activities for Week '+week+' '+day);
        dayButton.setAttribute('aria-pressed',String(active));
        dayButton.addEventListener('click',event=>{event.stopPropagation();choose();});cell.append(dayButton);
        const entries=assigned.flatMap(r=>slots(r).filter(s=>s.week===week&&s.day===d).map(()=>r));
        entries.forEach(r=>cell.append(card(r,week,d)));
        if(!entries.length)cell.append(e('span','—','bx-muted'));row.append(cell);
      });table.append(row);
    }
    wrap.append(table);root.append(wrap);
    const panel=e('section',null,'bx-day-activities');panel.id='bx-day-activities';
    if(!config.selectedDay){
      panel.append(e('p','Click a week and day above to see all activities for that day.'));
    }else{
      const {week,day}=config.selectedDay;
      const activities=runs.flatMap(r=>slots(r).filter(s=>s.week===week&&s.day===day).map(()=>r));
      panel.append(e('h3','Week '+week+' · '+days[day]+' activities'),
        e('p','All frequencies for this day are shown below, including runs outside the selected category.'));
      if(!activities.length)panel.append(e('p','No activities allocated to this day.'));
      else {
        const timeKeys=['drive_min','service_min','depot_min','prep_min','wait_min','break_min'];
        const kmUnknown=activities.some(r=>r.km==null);
        const timeUnknown=activities.some(r=>timeKeys.some(k=>r[k]==null));
        const km=activities.reduce((a,r)=>a+(r.km||0),0);
        const hours=activities.reduce((a,r)=>a+timeKeys.reduce((n,k)=>n+(r[k]||0),0),0)/60;
        panel.append(e('p',activities.length+' run occurrences · '+activities.reduce((a,r)=>a+r.site_ids.length,0)+' customer visits before load splits · '+fmt(km)+' km'+(kmUnknown?' known (incomplete)':'')+' · '+fmt(hours)+' hours'+(timeUnknown?' known (incomplete)':' estimated elapsed')));
        const labels={cage:'cages',bin660:'660L bins',bin240:'240L bins',bin120:'120L bins',pallecon:'pallecons'};
        const contents=c=>Object.entries(labels).filter(([k])=>c?.[k]).map(([k,l])=>fmt(c[k])+' '+l).join(', ')||'Quantity to confirm';
        activities.forEach((r,index)=>{
          const activity=e('article',null,'bx-day-activity');
          const badges=e('div',null,'bx-frequency-badges');
          groups(r,sites).forEach(c=>{const badge=e('span',c,'bx-frequency-label');badge.dataset.frequency=c;badges.append(badge);});activity.append(badges);
          activity.append(e('h4',(index+1)+'. '+r.name),e('p','Start / finish: '+(model.states[state].depot||'Depot to confirm')));
          if(r.runs_4w===0)activity.append(e('p','Paused run — review this allocation.','bx-warning'));
          const p=plans?.[state]?.[r.id];
          const rows=[];
          const required=Object.fromEntries(Object.keys(labels).map(k=>[k,r.site_ids.reduce((n,id)=>n+(sites.find(s=>s.id===id)?.containers?.[k]||0),0)]));
          activity.append(e('p','Empty stock required across this run: '+contents(required)+'. Match each container type. Unknown customer quantities are additional.'),
            e('p','Unload empties first, then load full containers. Full returns must be emptied before they can be reused. Confirm depot loading and customer swap time allowances.','bx-muted'));
          if(p&&!p.issues.length&&p.loads.length){
            p.loads.forEach((load,i)=>{
              const outbound=load.outbound_empty||Object.fromEntries(Object.keys(labels).map(k=>[k,load.stops.reduce((n,s)=>n+(s.containers[k]||0),0)]));
              rows.push(['Load '+(i+1)+' · Load empties',model.states[state].depot||'Depot to confirm','',contents(outbound)+' EMPTY']);
              load.stops.forEach(stop=>rows.push(['Load '+(i+1)+' · Switch out',stop.name,stop.address,'Deliver '+contents(stop.deliver_empty||stop.containers)+' EMPTY → collect '+contents(stop.collect_full||stop.containers)+' FULL'+(stop.onboard_empty_after?' | On board after: '+(Object.values(stop.onboard_empty_after).some(Boolean)?contents(stop.onboard_empty_after):'0')+' empty; '+contents(stop.onboard_full_after)+' full':'' )]));
              rows.push(['Load '+(i+1)+' · Return / unload',model.states[state].depot||'Depot to confirm','',contents(load.return_full||outbound)+' FULL · '+fmt(load.spaces)+' positions · '+fmt(load.spare_spaces)+' spare']);
            });
          }else r.site_ids.forEach(id=>{const s=sites.find(s=>s.id===id);if(s)rows.push(['Switch out',s.name,s.address,'Deliver '+contents(s.containers)+' EMPTY → collect same FULL']);});
          const scroll=e('div',null,'bx-scroll'),t=e('table',null,'bx-customers'),head=e('tr');
          ['Activity','Location','Address','What / quantity'].forEach(x=>head.append(e('th',x)));t.append(head);
          rows.forEach(values=>{const row=e('tr');values.forEach(x=>row.append(e('td',x||'—')));t.append(row);});scroll.append(t);activity.append(scroll);
          const duration=timeKeys.some(k=>r[k]==null)?null:timeKeys.reduce((a,k)=>a+r[k],0)/60;
          activity.append(e('p',fmt(r.km)+' km · '+fmt(duration)+' hours elapsed · '+r.status),
            e('p','Driving '+fmt(r.drive_min)+' min · Collections '+fmt(r.service_min)+' min · Depot '+fmt(r.depot_min)+' min · Prep '+fmt(r.prep_min)+' min · Waiting '+fmt(r.wait_min)+' min · Breaks '+fmt(r.break_min)+' min'),
            e('p',r.sequence||'Route sequence to confirm'));
          if(p?.issues.length)activity.append(e('p',p.issues.join('; '),'bx-warning'));
          if(p?.extra_loads)activity.append(e('p',p.extra_loads+' extra loads need updated distance/time allowances.','bx-warning'));
          const detail=e('button','Open / edit run details','secondary');detail.type='button';detail.addEventListener('click',()=>{config.dayOpen=false;onSelect(r.id);root.querySelector('.bx-selected-run')?.scrollIntoView({block:'start',behavior:'smooth'});});activity.append(detail);
          panel.append(activity);
        });
        panel.append(e('p','Activity order follows the saved run/load sequence. Run order within the day is not a timed dispatch schedule.','bx-muted'));
      }
    }
    if(config.selectedDay&&config.dayOpen){
      if(!popup){
        popup=e('div',null,'bx-modal-backdrop');popup.id='bx-day-popup';
        const dialog=e('div',null,'bx-day-dialog');dialog.id='bx-day-dialog';
        dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby','bx-day-dialog-title');dialog.tabIndex=-1;
        popup.append(dialog);document.body.append(popup);
        popup.addEventListener('pointerdown',event=>{if(event.target===popup)dismissPopup?.();});
        popup.addEventListener('keydown',event=>{
          if(event.key==='Escape'){event.preventDefault();dismissPopup?.();return;}
          if(event.key==='Tab'){
            const focusable=[...popup.querySelectorAll('button,a[href],input,select,textarea,[tabindex="0"]')].filter(n=>!n.disabled);
            const first=focusable[0],last=focusable[focusable.length-1];
            if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
            else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
          }
        });
      }
      const dialog=popup.querySelector('#bx-day-dialog'),wasHidden=popup.hidden||!dialog.children.length;
      const title=e('h2','Week '+config.selectedDay.week+' · '+days[config.selectedDay.day]+' summary');title.id='bx-day-dialog-title';
      const header=e('div',null,'bx-day-dialog-header'),close=e('button','Close ×','secondary');close.type='button';close.setAttribute('aria-label','Close day summary');
      dismissPopup=()=>{config.dayOpen=false;popup.hidden=true;document.body.classList.remove('bx-popup-open');root.querySelector('[data-week="'+config.selectedDay.week+'"][data-day="'+config.selectedDay.day+'"] .bx-view-day')?.focus({preventScroll:true});};
      close.addEventListener('click',()=>dismissPopup());
      const focusedLabel=popup.contains(document.activeElement)?document.activeElement.getAttribute('aria-label'):null;
      header.append(title,close);dialog.replaceChildren(header,panel);popup.hidden=false;document.body.classList.add('bx-popup-open');
      if(wasHidden||focusedLabel||document.activeElement===document.body)close.focus({preventScroll:true});
    }else if(popup){
      popup.hidden=true;document.body.classList.remove('bx-popup-open');
    }
    const pending=runs.filter(r=>!slots(r).length);
    const backlog=e('div',null,'bx-planner-backlog');backlog.append(e('h3','Needs allocation'));
    if(!pending.length)backlog.append(e('p','All runs have a calendar allocation.'));
    pending.forEach(r=>{const item=e('div');item.append(card(r));
      backlog.append(item);});root.append(backlog);

    const allocationRun=runs.find(r=>r.id===config.allocateId);
    if(allocationRun){
      if(!allocationPopup){allocationPopup=e('div',null,'bx-modal-backdrop');allocationPopup.id='bx-allocation-popup';document.body.append(allocationPopup);}
      const draft=config.allocationDraft||(config.allocationDraft={frequency:allocationRun.planner_frequency||(allocationRun.runs_4w===4?'Weekly':allocationRun.runs_4w===2?'Fortnightly':allocationRun.runs_4w===1?'Monthly':'Ad hoc'),day:slots(allocationRun)[0]?.day??0,week:slots(allocationRun)[0]?.week??1});
      const box=e('div',null,'bx-day-dialog bx-allocation-dialog');box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');box.setAttribute('aria-labelledby','bx-allocation-title');
      const header=e('div',null,'bx-day-dialog-header'),title=e('h2','Allocate this run');title.id='bx-allocation-title';
      const close=e('button','Cancel','secondary');close.type='button';header.append(title,close);box.append(header);
      const dismiss=()=>{config.allocateId=null;config.allocationDraft=null;allocationPopup.hidden=true;document.body.classList.remove('bx-popup-open');root.querySelector('[data-run-id="'+allocationRun.id+'"]')?.focus();};
      close.addEventListener('click',dismiss);
      const body=e('div',null,'bx-allocation-body');body.append(e('h3',allocationRun.name));
      const swapLabels={cage:'cages',bin660:'660L bins',bin240:'240L bins',bin120:'120L bins',pallecon:'pallecons'};
      const swaps=Object.entries(swapLabels).map(([k,label])=>{const count=allocationRun.site_ids.reduce((n,id)=>n+(sites.find(s=>s.id===id)?.containers?.[k]||0),0);return count?fmt(count)+' '+label:null;}).filter(Boolean).join(', ');
      body.append(e('p',swaps?'Each run requires '+swaps+' EMPTY for switch-outs, with matching FULL returns. Truckload splits appear in the day summary.':'Switch-out quantities need confirmation before loading.'));

      function field(label,key,options){
        const l=e('label',label),select=e('select');select.setAttribute('aria-label',label);
        options.forEach(([v,t])=>{const o=e('option',t);o.value=v;select.append(o);});select.value=draft[key];
        select.addEventListener('change',()=>{draft[key]=key==='frequency'?select.value:Number(select.value);updateHint();});l.append(select);body.append(l);return select;
      }
      field('Collection frequency','frequency',categories.map(c=>[c,c]));
      field('Collection day','day',days.map((d,i)=>[i,d]));
      const weekSelect=field('Starting week','week',[1,2,3,4].map(w=>[w,'Week '+w]));
      const hint=e('p',null,'bx-muted');body.append(hint);
      function updateHint(){weekSelect.disabled=draft.frequency==='Weekly';hint.textContent=draft.frequency==='Weekly'?'Every week on the selected day.':draft.frequency==='Fortnightly'?'Weeks '+(draft.week%2? '1 and 3':'2 and 4')+' on the selected day.':draft.frequency==='Monthly'?'Once per four-week cycle, in the selected week.':'One booking in the selected week. No recurring collection frequency.';}
      updateHint();body.append(e('p','This updates this run only. Customer frequency differences remain flagged for proposal review.','bx-muted'));
      const apply=e('button','Lock in allocation','primary');apply.type='button';
      apply.addEventListener('click',()=>{
        const blocked=sites.filter(s=>allocationRun.site_ids.includes(s.id)&&s.day_rule==='fixed'&&!s.service_days?.includes(draft.day));
        if(blocked.length){alert('Customer-set day conflict: '+blocked.map(s=>s.name).join(', '));return;}
        const weeks=draft.frequency==='Weekly'?[1,2,3,4]:draft.frequency==='Fortnightly'?(draft.week%2?[1,3]:[2,4]):[draft.week];
        allocationRun.planner_frequency=draft.frequency;
        allocationRun.planner_slots=weeks.map(week=>({week,day:draft.day}));
        allocationRun.runs_4w={Weekly:4,Fortnightly:2,Monthly:1,'Ad hoc':null}[draft.frequency];
        config.all=true;config.span=4;config.start=1;config.selectedDay={week:weeks[0],day:draft.day};
        config.allocateId=null;config.allocationDraft=null;allocationPopup.hidden=true;document.body.classList.remove('bx-popup-open');
        onChange();document.getElementById('bx-save')?.click();
      });body.append(apply);box.append(body);
      allocationPopup.replaceChildren(box);allocationPopup.hidden=false;document.body.classList.add('bx-popup-open');
      allocationPopup.onpointerdown=event=>{if(event.target===allocationPopup)dismiss();};
      allocationPopup.onkeydown=event=>{
        if(event.key==='Escape'){event.preventDefault();dismiss();}
        if(event.key==='Tab'){const fields=[...box.querySelectorAll('button,select')].filter(n=>!n.disabled),first=fields[0],last=fields[fields.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}
      };
      close.focus({preventScroll:true});
    }else if(allocationPopup){allocationPopup.hidden=true;}
    const r=runs.find(r=>r.id===selected);
    if(r){
      const box=e('details',null,'bx-planner-allocate');box.append(e('summary','Allocate / move this run'));
      box.append(e('p','Choose its day and cycle weeks, then Save model. This changes calendar allocation only; run frequency, costs and customer demand stay as entered.'));
      const label=e('label','Day'),day=e('select');day.setAttribute('aria-label','Allocation day');days.forEach((d,i)=>{const o=e('option',d);o.value=i;day.append(o);});day.value=slots(r)[0]?.day??0;label.append(day);box.append(label);
      const weeks=e('div',null,'bx-planner-controls'),checks=[];
      for(let w=1;w<=4;w++){const l=e('label','Week '+w),c=e('input');c.type='checkbox';c.checked=slots(r).some(s=>s.week===w);c.setAttribute('aria-label','Allocate week '+w);checks.push(c);l.prepend(c);weeks.append(l);}box.append(weeks);
      const apply=e('button','Apply allocation','secondary');apply.type='button';apply.addEventListener('click',()=>{const blocked=sites.filter(s=>r.site_ids.includes(s.id)&&s.day_rule==='fixed'&&!s.service_days?.includes(Number(day.value)));if(blocked.length){alert('Customer-set day conflict: '+blocked.map(s=>s.name).join(', '));return;}r.planner_slots=checks.flatMap((c,i)=>c.checked?[{week:i+1,day:Number(day.value)}]:[]);config.all=true;config.span=4;config.start=1;onChange();});box.append(apply);root.append(box);
    }
  }
  return {render,slots,groups};
};
