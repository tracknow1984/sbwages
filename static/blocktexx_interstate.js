window.BlocktexxInterstate=(()=>{
 const lanes={
  sydney_brisbane_bdouble:['Sydney → Brisbane · B-double','NSW'],
  melbourne_brisbane_bdouble:['Melbourne → Brisbane · B-double','VIC'],
  sydney_brisbane_semi:['Sydney → Brisbane · Semi','NSW'],
  melbourne_brisbane_semi:['Melbourne → Brisbane · Semi','VIC']
 };
 const fields=[['base_trip','Base $ / trip'],['fuel_pct','Fuel levy %'],['tolls_trip','Tolls $ / trip'],['other_trip','Other $ / trip'],['payload_kg','Usable payload kg']];
 const e=(t,v,c)=>{const n=document.createElement(t);if(v!=null)n.textContent=v;if(c)n.className=c;return n;};
 const fmt=n=>n==null?'TBC':Number(n).toLocaleString('en-AU',{maximumFractionDigits:2});
 const money=n=>n==null?'Incomplete':'$'+Number(n).toLocaleString('en-AU',{minimumFractionDigits:2,maximumFractionDigits:2});
 function ensure(model){
  const data=model.interstate||(model.interstate={lanes:{},bookings:[]});
  Object.keys(lanes).forEach(id=>data.lanes[id]??={base_trip:null,fuel_pct:null,tolls_trip:null,other_trip:null,payload_kg:null});
  return data;
 }
 function calculate(data){
  const results={};
  for(const [id,[name,state]] of Object.entries(lanes)){
   const p=data.lanes[id],rate=fields.slice(0,4).some(([k])=>p[k]==null)?null:p.base_trip*(1+p.fuel_pct/100)+p.tolls_trip+p.other_trip;
   const bookings=data.bookings.filter(b=>b.lane_id===id),trips=bookings.reduce((n,b)=>n+b.trips,0);
   const kg=bookings.some(b=>b.kg_trip==null)?null:bookings.reduce((n,b)=>n+b.kg_trip*b.trips,0);
   const cost=trips===0?0:rate==null?null:rate*trips;
   results[id]={name,state,trips,rate,kg,cost_4w:cost,monthly_cost:cost==null?null:cost*13/12,cost_per_kg:cost!=null&&kg?cost/kg:null};
  }
  const costs=Object.values(results).map(r=>r.cost_4w),total=costs.some(c=>c==null)?null:costs.reduce((a,c)=>a+c,0);
  return {lanes:results,cost_4w:total,monthly_cost:total==null?null:total*13/12};
 }
 function create(getModel,onChange,getSummaries){
  const root=document.getElementById('bx-interstate-content'),days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  let editing=null,inputs={},message,calendar,summary,form;
  function edit(b){
   editing=b?.id||null;
   for(const [key,input] of Object.entries(inputs))input.value=b?.[key]??({lane_id:'sydney_brisbane_bdouble',week:1,day:0,trips:1,transit_days:1}[key]??'');
   if(!b)originDefault();
   message.textContent=b?'Editing departure. Apply changes, then Save interstate.':'';
   form.querySelector('[type=submit]').textContent=b?'Update departure':'Add departure';
  }
  function originDefault(){
   const model=getModel(),state=lanes[inputs.lane_id.value][1];
   inputs.origin.value=model.states[state].depot||'';
   inputs.destination.value='';
  }
  function render(){
   const model=getModel(),data=ensure(model);root.replaceChildren();
   root.append(e('h2','Interstate transfers · national cost centre'),
     e('p','Dedicated B-double / semi departures, separate from local driver calendars. Enter the consolidated load, origin and delivery destination for each booking. The four-week cycle represents a repeating plan; transit days include the carrier’s planned rest and stops.'));
   const rates=e('details');rates.open=true;rates.append(e('summary','Profile interstate lane costs'));
   const scroll=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table'),head=e('tr');['Lane / truck',...fields.map(([,label])=>label)].forEach(label=>head.append(e('th',label)));table.append(head);
   for(const [id,[name]] of Object.entries(lanes)){
    const tr=e('tr');tr.append(e('th',name));
    for(const [key,label] of fields){
     const td=e('td'),input=e('input');input.type='number';input.min=0;input.max=key==='fuel_pct'?1000:1000000;input.step='any';input.value=data.lanes[id][key]??'';input.placeholder='Not set';input.setAttribute('aria-label',name+' '+label);
     input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}data.lanes[id][key]=input.value===''?null:Number(input.value);onChange();refresh();};td.append(input);tr.append(td);
    }table.append(tr);
   }
   scroll.append(table);rates.append(scroll,e('p','AUD excluding GST. Per-trip cost = base × (1 + fuel levy %) + tolls + other charges. Enter 0 where an item does not apply. Confirm payload with the carrier; no B-double or semi capacity is assumed.'));root.append(rates);
   form=e('form');form.className='bx-card';form.append(e('h3','Add / edit interstate departure'));const grid=e('div',null,'bx-settings');inputs={};
   function field(key,label,type,options){
    const wrap=e('label',label),input=e(options?'select':'input');
    if(options)options.forEach(([v,label])=>{const o=e('option',label);o.value=v;input.append(o);});
    else {input.type=type;if(type==='number'){input.min=key==='trips'?1:0;input.max=key==='trips'?50:key==='transit_days'?14:1000000;input.step=key==='kg_trip'?'any':1;}}
    input.required=!['kg_trip','notes'].includes(key);input.setAttribute('aria-label',label);wrap.append(input);grid.append(wrap);inputs[key]=input;
   }
   field('lane_id','Interstate lane','text',Object.entries(lanes).map(([id,[name]])=>[id,name]));
   field('week','Departure week','number',[1,2,3,4].map(n=>[n,'Week '+n]));
   field('day','Departure day','number',days.map((day,i)=>[i,day]));
   field('trips','Number of truck trips','number');
   field('kg_trip','Kilograms per truck trip','number');
   field('transit_days','Transit days including rest / stops','number');
   field('origin','Interstate origin address','text');field('destination','Interstate delivery address','text');field('notes','Load / carrier / delivery notes','text');
   inputs.lane_id.onchange=originDefault;
   message=e('p',null,'bx-warning');message.setAttribute('role','status');
   const apply=e('button','Add departure','primary');apply.type='submit';
   const cancel=e('button','Clear / new departure','secondary');cancel.type='button';cancel.onclick=()=>edit(null);
   form.append(grid,message,apply,cancel);
   form.onsubmit=event=>{
    event.preventDefault();if(!form.reportValidity())return;
    const b={id:editing||crypto.randomUUID()};
    Object.entries(inputs).forEach(([key,input])=>b[key]=['week','day','trips','kg_trip','transit_days'].includes(key)?(input.value===''?null:Number(input.value)):input.value.trim());
    const cap=data.lanes[b.lane_id].payload_kg;
    if(cap!=null&&b.kg_trip!=null&&b.kg_trip>cap){message.textContent='Load exceeds the configured payload for this truck option.';return;}
    const index=data.bookings.findIndex(r=>r.id===editing);if(index<0)data.bookings.push(b);else data.bookings[index]=b;
    onChange();edit(null);refresh();
   };
   root.append(form);calendar=e('section');summary=e('section');root.append(calendar,summary);edit(null);refresh();
  }
  function refresh(){
   if(!calendar)return;
   const model=getModel(),data=ensure(model),cost=calculate(data);calendar.replaceChildren(e('h3','Interstate departure calendar · four weeks'));
   const scroll=e('div',null,'bx-scroll'),table=e('table',null,'bx-planner-grid'),head=e('tr');
   ['Week',...days].forEach(day=>head.append(e('th',day)));table.append(head);
   for(let week=1;week<=4;week++){
    const row=e('tr');row.append(e('th','Week '+week));
    for(let day=0;day<7;day++){
     const td=e('td'),add=e('button','Add departure','secondary');add.type='button';add.onclick=()=>{edit(null);inputs.week.value=week;inputs.day.value=day;form.scrollIntoView({block:'start',behavior:'smooth'});};td.append(add);
     data.bookings.filter(b=>b.week===week&&b.day===day).forEach(b=>{
      const n=(b.week-1)*7+b.day+b.transit_days,arrival='Week '+(Math.floor(n/7)%4+1)+' '+days[n%7]+(n>=28?' (next cycle)':'');
      const card=e('button',null,'bx-interstate-booking');card.type='button';
      card.append(e('strong',lanes[b.lane_id][0]),e('small',b.trips+' truck trip(s) · '+fmt(b.kg_trip)+' kg each'),e('small',b.origin+' → '+b.destination),e('small','Arrival: '+arrival),
       e('small',money(cost.lanes[b.lane_id].rate==null?null:cost.lanes[b.lane_id].rate*b.trips)));
      card.onclick=()=>{edit(b);form.scrollIntoView({block:'start',behavior:'smooth'});};td.append(card);
      const remove=e('button','Remove','secondary');remove.type='button';remove.onclick=()=>{if(!confirm('Remove this interstate departure?'))return;data.bookings=data.bookings.filter(r=>r.id!==b.id);onChange();edit(null);refresh();};td.append(remove);
     });row.append(td);
    }table.append(row);
   }
   scroll.append(table);calendar.append(scroll);
   summary.replaceChildren(e('h3','Interstate cost centre summary'));
   const wrap=e('div',null,'bx-scroll'),t=e('table',null,'bx-resource-table'),h=e('tr');
   ['Lane','Truck trips / 4 weeks','Transferred kg / 4 weeks','All-in $ / trip','Cost / 4 weeks','Average cost / month','Freight cost / kg'].forEach(v=>h.append(e('th',v)));t.append(h);
   Object.values(cost.lanes).forEach(r=>{const tr=e('tr');[r.name,fmt(r.trips),fmt(r.kg),money(r.rate),money(r.cost_4w),money(r.monthly_cost),r.cost_per_kg==null?'Incomplete':'$'+r.cost_per_kg.toFixed(3)].forEach(v=>tr.append(e('td',v)));t.append(tr);});
   wrap.append(t);summary.append(wrap,e('p','Interstate total: '+money(cost.cost_4w)+' / four weeks · '+money(cost.monthly_cost)+' / average calendar month.'));
   const incomplete=data.bookings.filter(b=>data.lanes[b.lane_id].payload_kg==null||b.kg_trip==null||b.kg_trip>data.lanes[b.lane_id].payload_kg);
   if(incomplete.length)summary.append(e('p',incomplete.length+' departures need weight / payload review. Costs remain estimates.','bx-warning'));
   const local=getSummaries(),values=Object.values(local),localKnown=values.every(v=>v.cost!=null),localTotal=localKnown?values.reduce((n,v)=>n+v.cost,0):null;
   const complete=localKnown&&values.every(v=>!v.pending)&&cost.monthly_cost!=null&&!incomplete.length;
   const kgValues=Object.values(model.states).map(s=>s.monthly_kg),kg=kgValues.every(v=>v!=null)?kgValues.reduce((n,v)=>n+v,0):null;
   const combined=localTotal!=null&&cost.monthly_cost!=null?localTotal+cost.monthly_cost:null;
   summary.append(e('h3','Overall transport picture'),e('p','Local state operations: '+money(localTotal)+' / month · Interstate: '+money(cost.monthly_cost)+' / month · Combined: '+money(combined)+' / month'),
    e('p','Unique intake: '+fmt(kg)+' kg / month · Combined transport cost/kg: '+(complete&&kg?'$'+(combined/kg).toFixed(3):'Incomplete')),
    e('p','Intake kilograms come only from the state intake totals. Movement and interstate weights describe transferred stock and are never added to intake. Average calendar month = four weeks × 13 ÷ 12. These costs exclude processing/shredding fees, container costs and any unmodelled stages. No profit or margin is added yet.','bx-muted'));
  }
  return {render,refresh};
 }
 return {ensure,calculate,create,lanes};
})();
