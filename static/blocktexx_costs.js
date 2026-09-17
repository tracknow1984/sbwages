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
  function renderComparison(root,data,pending){
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
    const kg=e('tr');kg.append(e('th','Collection cost / kg'));[c.owned,c.hourly,c.daily].forEach(v=>kg.append(e('td',ready&&data.monthly_kg&&v!=null?'$'+(v/data.monthly_kg).toLocaleString('en-AU',{minimumFractionDigits:3,maximumFractionDigits:3}):'Incomplete')));table.append(kg);
    const saving=e('tr');saving.append(e('th','Monthly saving vs company'),e('td','—'));[c.hourly,c.daily].forEach(v=>saving.append(e('td',v==null||c.owned==null?'Incomplete':money(c.owned-v))));table.append(saving);wrap.append(table);root.append(wrap);
    root.append(e('p','Positive savings mean the contractor option costs less. Container purchase/rental, interstate freight, decommissioning and shredding are excluded unless you explicitly add an allowance.','bx-muted'));
  }
  return {calculate,renderInputs,renderComparison};
})();
