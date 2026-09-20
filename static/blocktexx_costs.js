window.BlocktexxCosts = (() => {
  const groups=[
    ['Full-time staff',[
      ['staff_qty','Full-time staff quantity',500,1],['staff_hourly','Staff rate $ / hour',10000],
      ['paid_hours_week','Paid hours / employee / week',168],['workers_comp_pct','Workers compensation % of wages',100],
      ['super_pct','Super / pension % of wages',100]]],
    ['Company truck and operating costs',[
      ['truck_insurance_month','Truck insurance $ / month',1e7],['truck_lease_month','Truck lease $ / month',1e7],
      ['fuel_month','Fuel $ / month',1e7],['owned_other_month','Other company costs $ / month',1e7]]],
    ['Building costs — included in both options',[
      ['building_insurance_month','Building insurance $ / month',1e7],['building_lease_month','Building lease $ / month',1e7]]],
    ['Contractor rates',[
      ['contractor_hourly','Contractor $ / hour',10000],['contractor_daily','Contractor $ / day',100000],
      ['minimum_hours','Minimum billed hours / day',24],['free_wait_minutes','Free waiting minutes / day',540],
      ['demurrage_hourly','Demurrage $ / hour',10000],['contractor_other_month','Other contractor costs $ / month',1e7]]]
  ];
  const money=n=>n==null?'Incomplete':'$'+Number(n).toLocaleString('en-AU',{minimumFractionDigits:2,maximumFractionDigits:2});
  const fmt=n=>Number(n).toLocaleString('en-AU',{maximumFractionDigits:2});
  const e=(t,v,c)=>{const n=document.createElement(t);if(v!=null)n.textContent=v;if(c)n.className=c;return n;};
  function profile(data){
    if(!data.cost_profile)data.cost_profile={enabled:false,contractor_basis:'hourly',contractor_hourly:data.hourly_rate??null,minimum_hours:data.minimum_hours??0,free_wait_minutes:30};
    return data.cost_profile;
  }
  function calculate(data){
    const p=profile(data),factor=13/12,buckets=new Map(),schedule=window.createBlocktexxPlanner();
    let schedule_complete=true,time_complete=true;
    for(const r of data.runs){
      const slots=schedule.slots(r),count=r.runs_4w;
      if(slots.length){
        if(count!=null&&count!==slots.length)schedule_complete=false;
        slots.forEach(s=>{const key=s.week+':'+s.day;if(!buckets.has(key))buckets.set(key,{runs:[],count:1});buckets.get(key).runs.push(r);});
      }else if(count){schedule_complete=false;buckets.set('run:'+r.id,{runs:[r],count});}
      else if(count==null)schedule_complete=false;
    }
    let days=0,hours=0,charged=0,baseHours=0;
    for(const b of buckets.values()){
      const keys=['drive_min','service_min','depot_min','prep_min','wait_min','break_min'];
      if(b.runs.some(r=>keys.some(k=>r[k]==null)))time_complete=false;
      const work=b.runs.reduce((n,r)=>n+keys.slice(0,4).reduce((a,k)=>a+(r[k]||0),0),0)/60;
      const wait=b.runs.reduce((n,r)=>n+(r.wait_min||0),0)/60;
      const breaks=b.runs.reduce((n,r)=>n+(r.break_min||0),0)/60;
      if(work+wait+breaks>9)schedule_complete=false;
      const free=(p.free_wait_minutes||0)/60;
      days+=b.count;hours+=(work+wait)*b.count;
      charged+=Math.max(0,wait-free)*b.count;
      baseHours+=Math.max(work+Math.min(wait,free),p.minimum_hours||0)*b.count;
    }
    const total=values=>values.some(v=>v==null)?null:values.reduce((a,v)=>a+v,0);
    const product=(...values)=>values.includes(0)?0:values.some(v=>v==null)?null:values.reduce((a,v)=>a*v,1);
    const wages=product(p.staff_qty,p.staff_hourly,p.paid_hours_week,52/12);
    const workers_comp=product(wages,p.workers_comp_pct,.01),super_cost=product(wages,p.super_pct,.01);
    const shared=total([p.building_insurance_month,p.building_lease_month]);
    const owned=total([wages,workers_comp,super_cost,shared,p.truck_insurance_month,p.truck_lease_month,p.fuel_month,p.owned_other_month]);
    const known=time_complete&&p.free_wait_minutes!=null;
    const demurrage=known?product(charged*factor,p.demurrage_hourly):null;
    const hourly_base=known&&p.minimum_hours!=null?product(baseHours*factor,p.contractor_hourly):null;
    const daily_base=product(days*factor,p.contractor_daily);
    const hourly=total([hourly_base,demurrage,shared,p.contractor_other_month]);
    const daily=total([daily_base,demurrage,shared,p.contractor_other_month]);
    return {owned,hourly,daily,wages,workers_comp,super_cost,shared,hourly_base,daily_base,demurrage,
      days_month:days*factor,hours_month:hours*factor,billed_hours_month:baseHours*factor,demurrage_hours_month:charged*factor,
      schedule_complete:schedule_complete&&time_complete,
      selected:data.cost_mode==='owned'?owned:data.cost_mode==='contractor'?(p.contractor_basis==='daily'?daily:hourly):null};
  }
  function renderInputs(root,data,onChange){
    const p=profile(data);root.replaceChildren();
    const label=e('label'),enabled=e('input');enabled.type='checkbox';enabled.checked=p.enabled;
    enabled.addEventListener('change',()=>{p.enabled=enabled.checked;onChange();});
    label.append(enabled,document.createTextNode(' Use this detailed cost profile for the proposal totals'));root.append(label);
    root.append(e('p','Blank means unpriced; enter 0 where a cost does not apply. Existing aggregate pricing stays active until you enable this profile. All figures are AUD excluding GST.','bx-muted'));
    if(data.fixed_monthly!=null)root.append(e('p','Previous company aggregate: '+money(data.fixed_monthly)+' / month (reference only when detailed pricing is enabled).','bx-muted'));
    groups.forEach(([title,fields])=>{
      const section=e('details');section.open=title==='Full-time staff';section.append(e('summary',title));
      const grid=e('div',null,'bx-settings');
      fields.forEach(([key,title,max,step])=>{
        const label=e('label',title),input=e('input');input.type='number';input.min=0;input.max=max;input.step=step||'any';input.value=p[key]??'';input.placeholder='Not priced';input.setAttribute('aria-label',title);
        input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}p[key]=input.value===''?null:Number(input.value);onChange();});
        label.append(input);grid.append(label);
      });
      section.append(grid);root.append(section);
    });
    const labelBasis=e('label','Contractor basis for proposal totals '),basis=e('select');basis.setAttribute('aria-label','Contractor pricing basis');
    [['hourly','Hourly'],['daily','Daily']].forEach(([value,title])=>{const o=e('option',title);o.value=value;basis.append(o);});basis.value=p.contractor_basis;
    basis.addEventListener('change',()=>{p.contractor_basis=basis.value;onChange();});labelBasis.append(basis);root.append(labelBasis);
    root.append(e('p','Staff wages = headcount × paid weekly hours × hourly rate × 52 ÷ 12. Workers compensation and super are editable percentages of base wages. Full-time wages remain payable on quiet collection days. Staff quantity affects costs; the calendar still represents one truck/driver schedule per state. Add leave relief, maintenance or other allowances in Other company costs.','bx-muted'),
      e('p','Contractors: same-state runs on the same calendar day share one day charge and one minimum. Hourly charges exclude breaks and waiting charged as demurrage. Free waiting is per day; excess waiting is charged at the demurrage rate in both contractor options. Daily rates are assumed to include the truck, fuel and normal work within the 9-hour window. Confirm these terms with the contractor.','bx-muted'));
  }
  function renderComparison(root,data,pending,scenario=null){
    const c=calculate(data),p=profile(data);root.replaceChildren(e('h3','Monthly cost comparison'));
    root.append(e('p',fmt(c.days_month)+' collection days / month · '+fmt(c.billed_hours_month)+' contractor base hours · '+fmt(c.demurrage_hours_month)+' demurrage hours. Four-week schedule × 13 ÷ 12.','bx-muted'));
    if(!p.enabled)root.append(e('p','Comparison preview only. Enable the detailed profile above to use it in proposal totals.','bx-warning'));
    const ready=!pending&&c.schedule_complete;
    if(!ready)root.append(e('p','Indicative comparison: collection gaps, incomplete timings or calendar allocations still need review. Cost/kg is withheld until these are resolved. Unallocated recurring runs are provisionally treated as separate attendances.','bx-warning'));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table'),head=e('tr');
    ['Cost / calendar month','Company operation','Contractor hourly','Contractor daily'].forEach(t=>head.append(e('th',t)));table.append(head);
    const rows=[
      ['Wages',c.wages,0,0],['Workers compensation',c.workers_comp,0,0],['Super / pension',c.super_cost,0,0],
      ['Truck insurance',p.truck_insurance_month,0,0],['Truck lease',p.truck_lease_month,0,0],['Fuel',p.fuel_month,0,0],
      ['Building lease + insurance',c.shared,c.shared,c.shared],['Contractor base charge',0,c.hourly_base,c.daily_base],
      ['Demurrage',0,c.demurrage,c.demurrage],['Other costs',p.owned_other_month,p.contractor_other_month,p.contractor_other_month],
      ['Total / month',c.owned,c.hourly,c.daily]];
    rows.forEach(([label,...values])=>{const row=e('tr');if(label==='Total / month')row.className='bx-resource-total';row.append(e('th',label));values.forEach(v=>row.append(e('td',money(v))));table.append(row);});
    const denominator=scenario?(scenario.missing?null:scenario.kg*13/12):data.monthly_kg;
    const kg=e('tr');kg.append(e('th',scenario?'Local cost / projected kg':'Local cost / historical kg'));[c.owned,c.hourly,c.daily].forEach(v=>kg.append(e('td',ready&&denominator&&v!=null?'$'+(v/denominator).toLocaleString('en-AU',{minimumFractionDigits:3,maximumFractionDigits:3}):'Incomplete')));table.append(kg);
    const saving=e('tr');saving.append(e('th','Monthly saving vs company'),e('td','—'));[c.hourly,c.daily].forEach(v=>saving.append(e('td',v==null||c.owned==null?'Incomplete':money(c.owned-v))));table.append(saving);wrap.append(table);root.append(wrap);
    root.append(e('p','Positive savings mean the contractor option costs less. Entered local transport movements are included. Container purchase/rental, processing and shredding fees are excluded unless added as an allowance. Interstate freight is costed separately in Interstate transfers.','bx-muted'));
  }

  function periodCosts(data,week,day=null) {
    const schedule=window.createBlocktexxPlanner();
    const filtered={...data,runs:data.runs.flatMap(r=>{
      const chosen=schedule.slots(r).filter(s=>(week==null||s.week===week)&&(day==null||s.day===day));
      return chosen.length?[{...r,planner_slots:chosen,runs_4w:chosen.length}]:[];
    })};
    const c=calculate(filtered),p=profile(data);
    const fixed=day!=null?12/364:week==null?12/13:12/52,variable=12/13;
    const scale=(n,f)=>n==null?null:n*f;
    const add=values=>values.some(v=>v==null)?null:values.reduce((a,v)=>a+v,0);
    const rows=[
      ['Staff wages',scale(c.wages,fixed),0,0],
      ['Workers compensation',scale(c.workers_comp,fixed),0,0],
      ['Super / pension',scale(c.super_cost,fixed),0,0],
      ['Truck insurance',scale(p.truck_insurance_month,fixed),0,0],
      ['Truck lease',scale(p.truck_lease_month,fixed),0,0],
      ['Fuel budget',scale(p.fuel_month,fixed),0,0],
      ['Building insurance',...Array(3).fill(scale(p.building_insurance_month,fixed))],
      ['Building lease',...Array(3).fill(scale(p.building_lease_month,fixed))],
      ['Contractor base charge',0,scale(c.hourly_base,variable),scale(c.daily_base,variable)],
      ['Demurrage',0,scale(c.demurrage,variable),scale(c.demurrage,variable)],
      ['Other costs',scale(p.owned_other_month,fixed),scale(p.contractor_other_month,fixed),scale(p.contractor_other_month,fixed)]
    ];
    return {rows,totals:[1,2,3].map(i=>add(rows.map(row=>row[i]))),
      days:c.days_month*variable,hours:c.hours_month*variable,billed:c.billed_hours_month*variable,
      demurrage:c.demurrage_hours_month*variable,complete:c.schedule_complete,
      occurrences:filtered.runs.reduce((n,r)=>n+r.planner_slots.length,0),
      unallocated:data.runs.filter(r=>!schedule.slots(r).length&&r.runs_4w!==0).length};
  }
  function dailySummary(data,week,day,sites=[]) {
    const schedule=window.createBlocktexxPlanner(),c=periodCosts(data,week,day),p=profile(data);
    const entries=data.runs.flatMap(r=>schedule.slots(r).filter(s=>s.week===week&&s.day===day).map(slot=>({run:r,slot})));
    const pickups=entries.filter(x=>!x.run.activity_type||x.run.activity_type==='collection');
    const weights=pickups.map(x=>window.BlocktexxWeights?window.BlocktexxWeights.pickup(x.run,x.slot,sites):{kg:x.slot.pickup_kg??0,missing:x.slot.pickup_kg==null?1:0});
    const missing=weights.reduce((n,x)=>n+x.missing,0),kg=weights.reduce((n,x)=>n+x.kg,0);
    const selected=data.cost_mode==='owned'?0:data.cost_mode==='contractor'?(p.contractor_basis==='daily'?2:1):null;
    // A priced result must use the enabled profile. Legacy budgets are visibly excluded.
    const values=selected==null?[]:c.rows.map(row=>row[selected+1]);
    const missingCosts=selected==null?[]:c.rows.filter(row=>row[selected+1]==null).map(row=>row[0]);
    const cost=p.enabled&&selected!=null?values.reduce((sum,value)=>sum+(value??0),0):null;
    const complete=c.complete&&missingCosts.length===0;
    return {week,day,kg,missing,missingCosts,cost,rate:kg>0&&cost!=null?cost/kg:null,complete,provisional:!complete||missing>0,occurrences:entries.length};
  }
  function periodSummary(data,week=null,sites=[]) {
    const days=(week==null?[1,2,3,4]:[week]).flatMap(w=>Array.from({length:7},(_,d)=>dailySummary(data,w,d,sites)));
    const kg=days.reduce((n,d)=>n+d.kg,0),missing=days.reduce((n,d)=>n+d.missing,0);
    const cost=days.some(d=>d.cost==null)?null:days.reduce((n,d)=>n+d.cost,0);
    const complete=days.every(d=>d.complete)&&!periodCosts(data,week).unallocated;
    return {days,kg,missing,cost,complete,missingCosts:[...new Set(days.flatMap(d=>d.missingCosts))],provisional:!complete||missing>0,rate:kg>0&&cost!=null?cost/kg:null};
  }
  const kgText=d=>fmt(d.kg)+' kg'+(d.missing?' + '+d.missing+' missing weights':'');
  const rateText=n=>n==null?'Unavailable':'$'+Number(n).toLocaleString('en-AU',{minimumFractionDigits:3,maximumFractionDigits:3})+' / kg';
  const summaryRateText=d=>d.kg<=0?'No pickup kg':d.cost==null?'Enable cost profile and select cost option':rateText(d.rate)+(d.provisional?' (provisional)':'');
  function renderCostWarning(root,d) {
    const notes=[];
    if(d.missingCosts.length)notes.push('Unpriced costs excluded: '+d.missingCosts.join(', ')+'.');
    if(d.missing)notes.push('Missing pickup weights are excluded from the kilogram total.');
    if(!d.complete&&!d.missingCosts.length)notes.push('The schedule or run timings still need review.');
    if(notes.length)root.append(e('p','Provisional cost/kg uses known costs ÷ known planned pickup kg. '+notes.join(' '),'bx-warning'));
  }
  function renderDailySummary(root,data,week,day,onChange,sites=[]) {
    const d=dailySummary(data,week,day,sites),cards=e('div',null,'bx-metrics');
    [['Planned pickup kg',kgText(d)],[d.provisional?'Known daily costs':'Total daily cost',money(d.cost)],['Daily cost / kg',summaryRateText(d)]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));cards.append(card);});root.append(cards);renderCostWarning(root,d);renderBreakEven(root,data,d);
    root.append(e('p','Calendar weights use historical averages unless overridden. Enter net material kg for each pickup below, excluding container weight. Costs include every local movement and 1/7 of weekly overheads; transferred material is not counted again.','bx-muted'));
    const schedule=window.createBlocktexxPlanner();
    data.runs.filter(r=>!r.activity_type||r.activity_type==='collection').forEach(r=>{
      schedule.slots(r).forEach((slot,index)=>{
        if(slot.week!==week||slot.day!==day)return;
        const label=e('label',r.name+' · total run kg override '),input=e('input');input.type='number';input.min=0;input.max=1000000;input.step='any';input.value=slot.pickup_kg??'';input.placeholder='Use customer estimates';input.setAttribute('aria-label',r.name+' net kg collected occurrence '+(index+1));
        input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}if(!Array.isArray(r.planner_slots))r.planner_slots=schedule.slots(r).map(s=>({...s}));r.planner_slots[index].pickup_kg=input.value===''?null:Number(input.value);onChange();});label.append(input);root.append(label);
        const weight=window.BlocktexxWeights?.pickup(r,slot,sites);if(weight)root.append(e('p',weight.source+' · '+fmt(weight.kg)+' kg'+(weight.missing?' + missing customer weights':''),'bx-muted'));
        r.site_ids.forEach(id=>{const site=sites.find(s=>s.id===id);if(!site)return;const field=e('label',site.name+' · pickup kg '),v=e('input');v.type='number';v.min=0;v.max=1000000;v.step='any';v.value=slot.pickup_by_site?.[id]??'';v.placeholder=site.scenario_pickup_kg??site.sample_pickup_kg??'No sample';v.disabled=slot.pickup_kg!=null;v.setAttribute('aria-label',r.name+' '+site.name+' pickup kg occurrence '+(index+1));v.onchange=()=>{if(!v.checkValidity()){v.reportValidity();return;}if(!Array.isArray(r.planner_slots))r.planner_slots=schedule.slots(r).map(s=>({...s}));const target=r.planner_slots[index];target.pickup_by_site={...(target.pickup_by_site||{}),[id]:v.value===''?null:Number(v.value)};onChange();};field.append(v);root.append(field);});
        const reset=e('button','Use customer estimates for this pickup','secondary');reset.type='button';reset.onclick=()=>{if(!Array.isArray(r.planner_slots))r.planner_slots=schedule.slots(r).map(s=>({...s}));delete r.planner_slots[index].pickup_kg;delete r.planner_slots[index].pickup_by_site;onChange();};root.append(reset);
      });
    });
    if(!profile(data).enabled)root.append(e('p','Enable the detailed cost profile to calculate known daily costs and cost/kg. Blank costs will be excluded and flagged.','bx-warning'));
  }
  function breakEven(data,summary){
    const rate=data.selling_per_kg,ready=summary.complete&&!summary.missing&&summary.cost!=null;
    return {target:summary.complete&&summary.cost!=null&&rate>0?summary.cost/rate:null,
      revenue:!summary.missing&&rate!=null?summary.kg*rate:null,
      result:ready&&rate!=null?summary.kg*rate-summary.cost:null};
  }
  function renderBreakEven(root,data,summary){
    const b=breakEven(data,summary),cards=e('div',null,'bx-metrics');
    [['Selling rate / kg',data.selling_per_kg==null?'Enter in sample scenarios':rateText(data.selling_per_kg)],['Break-even kg',b.target==null?'Set costs and selling rate':fmt(b.target)+' kg'],['Revenue',money(b.revenue)],['Surplus / shortfall',money(b.result)],['Additional kg to break even',b.target==null||summary.missing?'Incomplete':fmt(Math.max(0,b.target-summary.kg))+' kg']].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));cards.append(card);});root.append(cards);
  }
  function showPeriodReport(data,state,week,onClose,plans,sites) {
    let backdrop=document.getElementById('bx-finance-popup');
    const fresh=!backdrop;
    if(!backdrop){backdrop=e('div',null,'bx-modal-backdrop');backdrop.id='bx-finance-popup';document.body.append(backdrop);}
    const c=periodCosts(data,week),p=profile(data),dialog=e('div',null,'bx-day-dialog');
    dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby','bx-finance-title');
    const title=e('h2',state+' · '+(week==null?'Monthly financial analysis':'Week '+week+' financial analysis'));title.id='bx-finance-title';
    const header=e('div',null,'bx-day-dialog-header'),close=e('button','Close ×','secondary');close.type='button';
    const dismiss=()=>{backdrop.remove();document.body.classList.remove('bx-popup-open');onClose();};
    close.addEventListener('click',dismiss);header.append(title,close);dialog.append(header);
    const content=e('section',null,'bx-day-activities');
    content.append(e('p',week==null?'Planned month: complete four-week cycle (28 days). Average calendar-month equivalent is shown separately below.':'Planned costs for this calendar week, across every frequency and day, including weekends.'),
      e('p',c.occurrences+' run occurrences · '+fmt(c.days)+' collection days · '+fmt(c.hours)+' working hours · '+fmt(c.billed)+' contractor base hours · '+fmt(c.demurrage)+' demurrage hours.'));
    if(!p.enabled)content.append(e('p','Detailed cost profile is not enabled. This is a profile preview; previous aggregate pricing is not included.','bx-warning'));
    if(!c.complete||c.unallocated)content.append(e('p','Incomplete plan: '+c.unallocated+' unallocated runs are excluded. Missing timings or overbooked days must be resolved before relying on the totals.','bx-warning'));
    const slotter=window.createBlocktexxPlanner();
    const included=data.runs.filter(r=>slotter.slots(r).some(s=>week==null||s.week===week));
    const unresolved=included.filter(r=>plans?.[state]?.[r.id]?.issues?.length||plans?.[state]?.[r.id]?.extra_loads);
    const demandGaps=(sites||[]).filter(s=>s.state===state&&s.visits_4w!=null&&Math.abs(s.visits_4w-data.runs.filter(r=>r.site_ids.includes(s.id)).reduce((n,r)=>n+(r.runs_4w||0),0))>.001);
    if(unresolved.length||demandGaps.length)content.append(e('p',unresolved.length+' runs need truck capacity/time review; '+demandGaps.length+' customer frequencies differ from the route plan. These are planning estimates.','bx-warning'));
    const summary=periodSummary(data,week,sites),metrics=e('div',null,'bx-metrics');
    [['Planned pickup kg',kgText(summary)],[summary.provisional?'Known period costs':'Total period cost',money(summary.cost)],['Cost per planned pickup kg',summaryRateText(summary)]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));metrics.append(card);});content.append(metrics);renderCostWarning(content,summary);renderBreakEven(content,data,summary);
    const dailyWrap=e('div',null,'bx-scroll'),dailyTable=e('table',null,'bx-resource-table'),dailyHead=e('tr');
    ['Day','Net kg picked up','Known daily costs','Cost / kg'].forEach(t=>dailyHead.append(e('th',t)));dailyTable.append(dailyHead);
    summary.days.forEach(d=>{const row=e('tr');['Week '+d.week+' '+['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][d.day],kgText(d),money(d.cost),summaryRateText(d)].forEach(t=>row.append(e('td',t)));dailyTable.append(row);});dailyWrap.append(dailyTable);content.append(dailyWrap);
    content.append(e('p','Period cost/kg = known daily costs ÷ known planned pickup kg, including costs on zero-pickup days. Missing costs or weights make the rate provisional. Daily weights are entered in View day. Blank weights are incomplete; zero means no material collected. Rates are planning figures based on entered costs and weights. Local deliveries and decomm returns add costs but no new intake. Interstate costs remain separate.','bx-muted'));
    const selected=data.cost_mode==='owned'?0:data.cost_mode==='contractor'?(p.contractor_basis==='daily'?2:1):null;
    content.append(e('p','Proposal option: '+(selected==null?'not selected':['Company operation','Contractor hourly','Contractor daily'][selected])+(selected==null?'':' · '+money(c.totals[selected]))));
    const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table'),head=e('tr');
    ['Cost item','Company operation','Contractor hourly','Contractor daily'].forEach(t=>head.append(e('th',t)));table.append(head);
    const append=(label,values,total=false)=>{const row=e('tr',null,total?'bx-resource-total':null);row.append(e('th',label));values.forEach(v=>row.append(e('td',money(v))));table.append(row);};
    c.rows.forEach(([label,...values])=>append(label,values));append(week==null?'Total · four-week month':'Total · week',c.totals,true);
    if(week==null)append('Average calendar month · four weeks × 13 ÷ 12',c.totals.map(v=>v==null?null:v*13/12));
    wrap.append(table);content.append(wrap);
    content.append(e('p','AUD excluding GST. Monthly staff and overhead budgets are allocated at monthly × 12 ÷ 52 per week, including quiet weeks. Fuel is a budget allocation, not measured fuel usage. Contractor charges use only this period’s scheduled days and hours, with daily minimums and demurrage applied once per day.','bx-muted'),
      e('p','Blank prices remain incomplete. Uses current on-screen figures, including unsaved edits. Entered local transport movements are included. Container purchases/rental and processing/shredding fees are excluded unless included in Other costs. Interstate freight is shown in its separate cost centre.','bx-muted'),
      e('p','Surplus / shortfall is scenario revenue less the selected local costs. It is not whole-contract profit. Higher loads may require additional truck time and cost.','bx-muted'));
    dialog.append(content);backdrop.replaceChildren(dialog);document.body.classList.add('bx-popup-open');
    backdrop.onpointerdown=event=>{if(event.target===backdrop)dismiss();};
    backdrop.onkeydown=event=>{
      if(event.key==='Escape'){event.preventDefault();dismiss();}
      if(event.key==='Tab'){event.preventDefault();close.focus();}
    };
    if(fresh||!dialog.contains(document.activeElement))close.focus({preventScroll:true});
  }
  return {calculate,renderInputs,renderComparison,periodCosts,dailySummary,periodSummary,breakEven,renderDailySummary,kgText,rateText,summaryRateText,showPeriodReport};
})();

