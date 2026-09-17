window.createBlocktexxPlanner = function() {
  const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const categories=['Weekly','Fortnightly','Monthly','Ad hoc'];
  const settings={};
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
    if(r.runs_4w==null||r.runs_4w===0)return ['Ad hoc'];
    const values=sites.filter(s=>r.site_ids.includes(s.id)).map(s=>s.visits_4w);
    const result=new Set(values.map(v=>v==null?'Ad hoc':v>=4?'Weekly':v>=2?'Fortnightly':'Monthly'));
    if(!result.size)result.add(r.runs_4w>=4?'Weekly':r.runs_4w>=2?'Fortnightly':'Monthly');
    return [...result];
  }
  function render(root,model,state,selected,onSelect,onChange) {
    const config=settings[state]||(settings[state]={category:'Weekly',span:4,start:1,all:false});
    const runs=model.states[state].runs,sites=model.sites;
    const refresh=()=>onSelect(selected);
    root.append(e('h2','Collection planner'));
    const tabs=e('div',null,'bx-state-tabs');
    categories.forEach(category=>{const b=e('button',category);b.type='button';b.setAttribute('aria-pressed',String(config.category===category));b.addEventListener('click',()=>{config.category=category;config.all=false;onSelect(null);});tabs.append(b);});
    root.append(tabs);
    const bar=e('div',null,'bx-planner-controls');
    const view=e('label','Planner view'),select=e('select');select.setAttribute('aria-label','Planner view');
    [[1,'Week'],[2,'Fortnight'],[4,'Month (4-week cycle)']].forEach(([v,t])=>{const o=e('option',t);o.value=v;select.append(o);});select.value=config.span;
    select.addEventListener('change',()=>{config.span=Number(select.value);config.start=1;refresh();});view.append(select);bar.append(view);
    if(config.span<4){const label=e('label','Cycle period'),period=e('select');period.setAttribute('aria-label','Cycle period');
      (config.span===1?[1,2,3,4]:[1,3]).forEach(w=>{const o=e('option',config.span===1?'Week '+w:'Weeks '+w+'–'+(w+1));o.value=w;period.append(o);});
      period.value=config.start;period.addEventListener('change',()=>{config.start=Number(period.value);refresh();});label.append(period);bar.append(label);}
    const all=e('label'),checkbox=e('input');checkbox.type='checkbox';checkbox.checked=config.all;checkbox.addEventListener('change',()=>{config.all=checkbox.checked;refresh();});all.append(checkbox,document.createTextNode(' Show all frequencies together'));bar.append(all);root.append(bar);
    root.append(e('p','Select a run card for pickups, quantities, kilometres and load details. Mixed-frequency runs appear in each relevant category. Monthly is a four-week planning cycle; eight-weekly work is labelled separately.','bx-muted'));
    const visible=runs.filter(r=>config.all||groups(r,sites).includes(config.category));
    function card(r) {
      const b=e('button',null,'bx-planner-run');b.type='button';b.dataset.runId=r.id;b.setAttribute('aria-pressed',String(r.id===selected));
      const name=r.name.replace(/^Week\s+\d+\s+/i,'').replace(/^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Weekly|Fortnightly)\s*—?\s*/i,'');
      const minutes=['drive_min','service_min','depot_min','prep_min','wait_min','break_min'];
      const hours=minutes.some(k=>r[k]==null)?null:minutes.reduce((n,k)=>n+r[k],0)/60;
      b.append(e('strong',name),e('small',r.site_ids.length+' locations · '+fmt(r.km)+' km · '+fmt(hours)+' h'));
      if(r.runs_4w===.5)b.append(e('small','Every 8 weeks — allocate only when due'));
      if(r.runs_4w===0)b.append(e('small','Paused'));
      if(groups(r,sites).length>1)b.append(e('small','Mixed: '+groups(r,sites).join(' / ')));
      b.addEventListener('click',()=>onSelect(r.id));return b;
    }
    const assigned=visible.filter(r=>slots(r).length),hasWeekend=assigned.some(r=>slots(r).some(s=>s.day>4));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-planner-grid'),head=e('tr');
    head.append(e('th','Week'));days.slice(0,hasWeekend?7:5).forEach(day=>head.append(e('th',day)));table.append(head);
    for(let week=config.start;week<config.start+config.span;week++){
      const row=e('tr');row.append(e('th','Week '+week));
      days.slice(0,hasWeekend?7:5).forEach((day,d)=>{
        const cell=e('td');cell.dataset.week=week;cell.dataset.day=d;
        const entries=assigned.flatMap(r=>slots(r).filter(s=>s.week===week&&s.day===d).map(()=>r));
        entries.forEach(r=>cell.append(card(r)));
        if(!entries.length)cell.append(e('span','—','bx-muted'));row.append(cell);
      });table.append(row);
    }
    wrap.append(table);root.append(wrap);
    const pending=visible.filter(r=>!slots(r).length||r.runs_4w==null||slots(r).length!==r.runs_4w);
    const backlog=e('div',null,'bx-planner-backlog');backlog.append(e('h3',config.category==='Ad hoc'&&!config.all?'Ad hoc / awaiting booking':'Needs allocation or review'));
    if(!pending.length)backlog.append(e('p','All runs in this view have their planned occurrences allocated.'));
    pending.forEach(r=>{const item=e('div');item.append(card(r));
      if(slots(r).length)item.append(e('small',slots(r).length+' allocated vs '+fmt(r.runs_4w)+' occurrences / four weeks. Review frequency.'));
      backlog.append(item);});root.append(backlog);
    const r=runs.find(r=>r.id===selected);
    if(r){
      const box=e('details',null,'bx-planner-allocate');box.append(e('summary','Allocate / move this run'));
      box.append(e('p','Choose its day and cycle weeks, then Save model. This changes calendar allocation only; run frequency, costs and customer demand stay as entered.'));
      const label=e('label','Day'),day=e('select');day.setAttribute('aria-label','Allocation day');days.forEach((d,i)=>{const o=e('option',d);o.value=i;day.append(o);});day.value=slots(r)[0]?.day??0;label.append(day);box.append(label);
      const weeks=e('div',null,'bx-planner-controls'),checks=[];
      for(let w=1;w<=4;w++){const l=e('label','Week '+w),c=e('input');c.type='checkbox';c.checked=slots(r).some(s=>s.week===w);c.setAttribute('aria-label','Allocate week '+w);checks.push(c);l.prepend(c);weeks.append(l);}box.append(weeks);
      const apply=e('button','Apply allocation','secondary');apply.type='button';apply.addEventListener('click',()=>{r.planner_slots=checks.flatMap((c,i)=>c.checked?[{week:i+1,day:Number(day.value)}]:[]);onChange();});box.append(apply);root.append(box);
    }
  }
  return {render,slots,groups};
};
