window.BlocktexxNational=(()=>{
 const factor=13/12,kinds={bin120:'120L bins',bin240:'240L bins',bin660:'660L bins',cage:'Cages',pallecon:'Pallecons'};
 const valid=n=>typeof n==='number'&&Number.isFinite(n);
 const sum=a=>a.reduce((n,v)=>n+(valid(v)?v:0),0);
 function calculate(model){
  const costs=window.BlocktexxCosts,schedule=window.createBlocktexxPlanner(),lines=[],issues=[],states=[],resources=[],freight=[];
  const add=(state,category,value,note='')=>{lines.push({state,category,value:valid(value)?value:null,note});if(!valid(value))issues.push(state+' · '+category+': amount not entered or incomplete');};
  for(const [state,d] of Object.entries(model.states)){
   const period=costs.periodSummary(d,null,model.sites),sites=model.sites.filter(s=>s.state===state);
   const occurrences=d.runs.flatMap(r=>schedule.slots(r).map(slot=>({run:r,slot}))).filter(x=>!x.run.activity_type||x.run.activity_type==='collection');
   const knownWeight=occurrences.some(x=>{const w=window.BlocktexxWeights.pickup(x.run,x.slot,model.sites);return w.kg>0||!w.missing;});
   const calendar=occurrences.length>0&&knownWeight;
   const kg=calendar?period.kg*factor:valid(d.monthly_kg)?d.monthly_kg:null;
   const basis=calendar?'Calendar pickup weights × 13/12':'Historical monthly intake';
   if(kg==null)issues.push(state+' · incoming kilograms not entered');
   if(calendar&&period.missing)issues.push(state+' · '+period.missing+' pickup weights missing; known kg only');
   if(!calendar)issues.push(state+' · using historical intake; calendar weights not established');
   const unallocated=d.runs.filter(r=>r.runs_4w!==0&&!schedule.slots(r).length).length;
   if(unallocated)issues.push(state+' · '+unallocated+' unallocated runs/movements; scheduled costs may be incomplete');
   if(d.cost_profile?.enabled&&d.cost_mode!=='unpriced'){
    const rows=new Map();
    for(const day of period.days)for(const row of day.rows){const entry=rows.get(row.label)||{value:0,missing:false};entry.value+=valid(row.value)?row.value*factor:0;entry.missing||=!valid(row.value);rows.set(row.label,entry);}
    for(const [label,row] of rows){add(state,label,row.value,row.missing?'Known subtotal; some amounts missing':'');if(row.missing)issues.push(state+' · '+label+': missing amounts excluded');}
    if(!period.complete)issues.push(state+' · calendar, timings or costs require review');
   }else if(d.cost_mode==='owned')add(state,'Company transport · aggregate',d.fixed_monthly,'Detailed cost profile disabled');
   else if(d.cost_mode==='contractor'){
    const grouped=new Map();
    for(const r of d.runs){const slots=schedule.slots(r),count=slots.length||r.runs_4w||0;if(!count)continue;const h=['drive_min','service_min','depot_min','prep_min','wait_min'].every(k=>valid(r[k]))?sum(['drive_min','service_min','depot_min','prep_min','wait_min'].map(k=>r[k]))/60:null;
     if(h==null){issues.push(state+' · '+r.name+': contractor hours missing');continue;}
     if(slots.length)for(const slot of slots){const key=slot.week+':'+slot.day;grouped.set(key,{hours:(grouped.get(key)?.hours||0)+h,count:1});}else grouped.set('run:'+r.id,{hours:h,count});
    }
    add(state,'Contractor transport · aggregate',valid(d.hourly_rate)?sum([...grouped.values()].map(b=>Math.max(b.hours,d.minimum_hours||0)*b.count))*d.hourly_rate*factor:null,'Detailed cost profile disabled');
   }else add(state,'Local/decom/production transport not priced',null);
   const local=sum(lines.filter(r=>r.state===state).map(r=>r.value));
   let rental=0,purchase=0;
   for(const [kind,label] of Object.entries(kinds)){
    const p=d.resource_pricing?.[kind]||{},required=2*sum(sites.map(s=>s.containers?.[kind])),qty=p.rental_qty??required;
    const rent=qty===0?0:valid(p.weekly_rent_each)?qty*p.weekly_rent_each*52/12:null;
    const buy=required===0?0:valid(p.purchase_each)?required*p.purchase_each:null;
    resources.push({state,label,required,qty,rent,buy});rental+=rent||0;purchase+=buy||0;
    add(state,'Container rental · '+label,rent,'Rental scenario');
    if(buy==null)issues.push(state+' · '+label+': one-off purchase price missing');
   }
   if(sites.some(s=>!Object.keys(kinds).some(k=>s.containers?.[k]>0)))issues.push(state+' · some customer container quantities are unknown');
   let equipment=0;
   if(state==='NSW')for(const [kind,label] of [['baler','Baler'],['forklift','Forklift']]){
    const b=d.resource_equipment?.[kind];if(!b)continue;
    const lease=b.quantity===0?0:valid(b.quantity)&&valid(b.monthly_lease_each)?b.quantity*b.monthly_lease_each:null;
    add(state,label+' equipment lease',lease,'Monthly lease; purchase value is reference only');equipment+=lease||0;
   }
   const visits=sum(d.runs.filter(r=>!r.activity_type||r.activity_type==='collection').map(r=>schedule.slots(r).length))*factor;
   states.push({state,kg,basis,local,rental,equipment,purchase,visits,mode:costs.modeLabel(d.cost_mode)});
  }
  for(const [id,lane] of Object.entries(model.interstate?.lanes||{})){
   const trips=sum((model.interstate.bookings||[]).filter(b=>b.lane_id===id).map(b=>b.trips))*factor;if(!trips)continue;
   const name=window.BlocktexxInterstate.lanes[id]?.[0]||id;
   const values=[valid(lane.base_trip)?lane.base_trip*trips:null,valid(lane.base_trip)&&valid(lane.fuel_pct)?lane.base_trip*lane.fuel_pct/100*trips:null,valid(lane.tolls_trip)?lane.tolls_trip*trips:null,valid(lane.other_trip)?lane.other_trip*trips:null];
   ['Freight base','Freight fuel levy','Freight tolls','Freight other'].forEach((label,i)=>add('National',label,values[i],name));
   freight.push({name,trips,values,total:sum(values),incomplete:values.some(v=>v==null)});
  }
  const s=model.storage||{};
  const storage=valid(s.total_containers)&&valid(s.free_containers)&&valid(s.monthly_rate)?Math.max(0,s.total_containers-s.free_containers)*s.monthly_rate:null;
  const repack=valid(s.occupied_containers)&&valid(s.repack_cost)?s.occupied_containers*s.repack_cost:null;
  const kg=sum(states.map(s=>s.kg)),local=sum(states.map(s=>s.local)),rental=sum(states.map(s=>s.rental)),interstate=sum(freight.map(r=>r.total));
  const equipment=sum(states.map(s=>s.equipment)),transport=local+interstate,total=transport+rental+equipment,purchase=sum(states.map(s=>s.purchase));
  return {states,lines,resources,freight,issues:[...new Set(issues)],kg,local,rental,equipment,interstate,storage,repack,purchase,transport,total,rate:kg>0?total/kg:null,transportRate:kg>0?transport/kg:null};
 }
 const e=(t,v,c)=>{const n=document.createElement(t);if(v!=null)n.textContent=v;if(c)n.className=c;return n;};
 const num=n=>n==null?'Not entered':Number(n).toLocaleString('en-AU',{maximumFractionDigits:1});
 const money=n=>n==null?'Not entered':'$'+Number(n).toLocaleString('en-AU',{minimumFractionDigits:2,maximumFractionDigits:2});
 const rate=n=>n==null?'No intake kg':'$'+Number(n).toFixed(3)+' / kg';
 function table(root,headers,rows){const wrap=e('div',null,'bx-scroll'),t=e('table',null,'bx-resource-table'),h=e('tr');headers.forEach(v=>h.append(e('th',v)));t.append(h);rows.forEach(values=>{const r=e('tr');values.forEach((v,i)=>r.append(e(i===0?'th':'td',v)));t.append(r);});wrap.append(t);root.append(wrap);}
 function issueTargets(message,model){
  const state=['QLD','NSW','VIC','SA'].find(s=>message.startsWith(s+' · '));
  const target=(pane,id,label,extra={})=>({state,pane,id,label,...extra});
  const planner=(scope='local')=>target(scope==='local'?'planner':scope,'bx-run-view',(state||'')+' '+(scope==='local'?'Local':scope==='decom'?'Decomm':'Production')+' Planner');
  if(message.includes('Storage contract'))return [target('storage','bx-storage','Storage assumptions',{expand:true})];
  if(message.includes('Freight '))return [target('interstate','bx-interstate-content','Interstate rates',{expand:true})];
  if(/purchase price|Container rental|Baler equipment|Forklift equipment|container quantities/.test(message)){
   const kind=Object.entries(kinds).find(([,label])=>message.includes(label))?.[0];
   return [target('resources','bx-resource-content',(state||'')+' Resources',{kind,expand:message.includes('container quantities')})];
  }
  if(/kilograms|pickup weights|calendar weights/.test(message))return [target('weights','bx-weights',(state||'')+' sample weights',{expand:true}),planner()];
  if(message.includes('unallocated')){
   const runs=model.states[state]?.runs||[],schedule=window.createBlocktexxPlanner();
   return ['local','decom','production'].filter(scope=>window.BlocktexxPlannerScope.runs(runs,scope).some(r=>r.runs_4w!==0&&!schedule.slots(r).length)).map(scope=>({...planner(scope),backlog:true}));
  }
  if(message.includes('contractor hours missing')){
   const run=model.states[state]?.runs.find(r=>message.includes(' · '+r.name+':'));
   const scope=run&&window.BlocktexxPlannerScope.isDecom(run)?'decom':run&&window.BlocktexxPlannerScope.isProduction(run)?'production':'local';
   return [{...planner(scope),id:'bx-all-runs',expand:true}];
  }
  const cost=target('planner',message.includes('aggregate')?'bx-legacy-costs':'bx-cost-inputs',(state||'')+' cost settings',{expand:true});
  if(message.includes('calendar, timings or costs'))return [cost,...['local','decom','production'].map(scope=>({...planner(scope),id:'bx-all-runs',expand:true}))];
  return [cost];
 }
 function renderCosts(root,model){
  const a=calculate(model);root.replaceChildren(e('h2','National Overview · monthly dashboard'),e('p','All states · AUD excluding GST · average calendar month'));
  const metrics=e('div',null,'bx-metrics');
  [[a.issues.length?'Known monthly recurring costs':'Monthly recurring costs',money(a.total)],['Incoming kilograms / month',num(a.kg)+' kg'],['Combined cost per kg'+(a.issues.length?' · provisional':''),rate(a.rate)],['Transport only / kg',rate(a.transportRate)]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));metrics.append(card);});root.append(metrics);
  root.append(e('p','Combined rate = (local/decom/production transport + booked interstate freight + container rental scenario + equipment leases) ÷ incoming kg. Storage is separate and excluded from this total and cost/kg. Transfers do not add intake kilograms. Figures are entered model costs and budgets, not verified invoice actuals.','bx-muted'));
  root.append(e('h3','Monthly cost breakdown'));
  table(root,['Cost category','Known monthly cost','Per incoming kg','Share of known cost'],[['Local, decom and production transport',a.local],['Interstate freight including fuel levy',a.interstate],['Container rentals · rental scenario',a.rental],['Equipment leases',a.equipment],['Total recurring costs',a.total]].map(([name,value])=>[name,money(value),value==null?'Not entered':rate(a.kg>0?value/a.kg:null),value==null||!a.total?'—':(value/a.total*100).toFixed(1)+'%']));
  root.append(e('h3','State comparison · monthly'));
  table(root,['State / operator','Incoming kg','Local + decom + production','Container rental','Equipment lease','State subtotal','State cost / kg','Kilogram basis'],a.states.map(s=>[s.state+' · '+s.mode,num(s.kg),money(s.local),money(s.rental),money(s.equipment),money(s.local+s.rental+s.equipment),rate(s.kg>0?(s.local+s.rental+s.equipment)/s.kg:null),s.basis]));
  root.append(e('p','State subtotals exclude interstate freight, which is added once nationally. Storage is shown separately and excluded from recurring costs and cost/kg. The national rate uses total costs ÷ total kg, not an average of state rates.','bx-muted'));
  root.append(e('h3','Detailed operating costs · monthly'));
  const categories=[...new Set(a.lines.map(r=>r.category))];
  table(root,['Expense','QLD','NSW','VIC','SA','National only','Known total / month'],categories.map(category=>{
   const values=['QLD','NSW','VIC','SA','National'].map(s=>{const rows=a.lines.filter(r=>r.category===category&&r.state===s);return rows.length?money(sum(rows.map(r=>r.value)))+(rows.some(r=>r.value==null||r.note.startsWith('Known subtotal'))?' *':''):'—';});
   return [category,...values,money(sum(a.lines.filter(r=>r.category===category).map(r=>r.value)))];
  }));
  root.append(e('h3','Interstate freight · monthly booked trips'));
  if(a.freight.length)table(root,['Route','Trips / month','Base','Fuel levy','Tolls','Other','Known total'],a.freight.map(r=>[r.name,num(r.trips),...r.values.map(money),money(r.total)+(r.incomplete?' *':'')]));
  else root.append(e('p','No interstate departures booked. Monthly interstate cost is $0 until trips are scheduled.'));
  const inventory=e('details');inventory.append(e('summary','Container quantities, monthly rentals and purchase costs'));
  table(inventory,['State','Container','Required · two sets','Rental qty','Rental / month','Purchase · one-off'],a.resources.map(r=>[r.state,r.label,num(r.required),num(r.qty),money(r.rent),money(r.buy)]));root.append(inventory);
  root.append(e('h3','One-off costs · separate from monthly rate'));
  table(root,['Item','Known amount'],[['Container purchase scenario',money(a.purchase)]]);
  root.append(e('p','Purchase and rental are alternative resource scenarios. Monthly totals use rental amounts; purchases and repacking are not also charged monthly. No depreciation or repayment term is assumed. Check that storage and rentals have not also been entered under other operating costs. Processing/shredding and unentered charges are excluded.','bx-muted'));
  root.append(e('h3','Storage · separate cost centre'));
  table(root,['Storage item','Amount · ex GST'],[['Storage contract / month',money(a.storage)],['Storage contract / year',money(a.storage==null?null:a.storage*12)],['Repacking · one-off',money(a.repack)]]);
  root.append(e('p','Storage rent and repacking are separate from the operating costs and cost/kg above.','bx-muted'));
  const storageLink=e('a',a.storage==null?'Complete storage assumptions →':'View / edit storage →');storageLink.href='#bx-storage';storageLink.onclick=event=>{event.preventDefault();document.dispatchEvent(new CustomEvent('bx-navigate',{detail:{pane:'storage',id:'bx-storage',expand:true}}));};root.append(storageLink);
  if(a.issues.length){const gaps=e('details');gaps.open=true;gaps.append(e('summary',a.issues.length+' items affecting completeness'));const list=e('ul');a.issues.forEach(v=>{
    const item=e('li');const targets=issueTargets(v,model);
    targets.forEach((target,index)=>{
      const link=e('a',index?' · '+target.label:v+' → '+target.label);link.href='#'+target.id;
      link.onclick=event=>{event.preventDefault();document.dispatchEvent(new CustomEvent('bx-navigate',{detail:target}));};item.append(link);
    });if(!targets.length)item.append(e('span',v));list.append(item);
   });gaps.append(e('p','Missing amounts are excluded from known subtotals, not treated as confirmed zero. The cost/kg above remains available using known costs and known intake.'),list);root.append(gaps);}
  root.append(e('p','Monthly conversion: four-week calendar × 13 ÷ 12; weekly rental × 52 ÷ 12. Existing monthly budgets stay monthly. Change inputs in the state planners, Resources, Interstate transfers or Storage; this dashboard recalculates from those entries.','bx-muted'));
 }
 function render(root,model){
  if(!window.BlocktexxPricing){renderCosts(root,model);return;}
  const a=calculate(model),p=window.BlocktexxPricing.calculate(a);
  const privateWasOpen=!!root.querySelector?.('.bx-private-costings[open]');
  root.replaceChildren(e('p','NATIONAL PROVISIONAL PRICING','bx-kicker'),e('h2','National Overview · provisional costings'),e('p','Monthly proposal · AUD · based on the current operating plan'));
  const metrics=e('div',null,'bx-metrics bx-provisional-metrics');
  [['Provisional monthly price · ex GST',money(p?.total??null)],['Provisional rate · ex GST',rate(p?.rate??null)],['Incoming kg / month',num(a.kg)+' kg'],['Provisional monthly price · incl GST',money(p?.includingGST??null)]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));metrics.append(card);});root.append(metrics);
  root.append(e('p','Provisional prices cover the modelled operating scope and booked movements. Storage and one-off repacking are separate. Missing costs and unallocated work remain excluded; these figures require scope and capacity confirmation before issue.','bx-warning'));
  if(!p)root.append(e('p','Complete private pricing settings before using the proposed figures.','bx-warning'));
  root.append(e('h3','Provisional monthly pricing breakdown'));
  table(root,['Included service','Monthly · ex GST','Per incoming kg · ex GST'],[['Local, decomm and production transport',p?.local],['Booked interstate freight',p?.interstate],['Container rental scenario',p?.rental],['Equipment provision',p?.equipment],['Total operating proposal',p?.total]].map(([name,value])=>[name,money(value??null),rate(a.kg>0&&valid(value)?value/a.kg:null)]));
  root.append(e('p','These categories form the total proposal above and are not additional charges. Kilograms are counted at intake once; later transfers do not create extra incoming kg.','bx-muted'));
  root.append(e('h3','Provisional state pricing · monthly'));
  table(root,['State / operator','Incoming kg','Provisional price · ex GST','Provisional rate / kg','Kilogram basis'],(p?.states||a.states).map(s=>[s.state+' · '+s.mode,num(s.kg),money(s.total??null),rate(s.rate??null),s.basis]));
  root.append(e('p','State comparisons allocate booked national interstate pricing in proportion to incoming kg. This is a comparison allowance, not a lane-specific freight quote. The national rate is the total proposed price divided by total intake.','bx-muted'));
  if(p?.unallocatedFreight)root.append(e('p','Unallocated interstate pricing: '+money(p.unallocatedFreight)+' / month. Enter intake kg to allocate it across states.','bx-warning'));
  root.append(e('h3','GST summary'));
  table(root,['Item','Monthly amount'],[['Operating proposal · ex GST',money(p?.total??null)],['GST',money(p?.gst??null)],['Operating proposal · incl GST',money(p?.includingGST??null)]]);
  root.append(e('h3','Storage · separate cost centre'));
  table(root,['Storage item','Amount · ex GST'],[['Storage contract / month',money(a.storage)],['Repacking · one-off',money(a.repack)]]);
  root.append(e('p','Storage and repacking are excluded from the operating proposal and operating rate per kilogram.','bx-muted'));
  const review=e('details',null,'bx-provisional-review');review.append(e('summary',a.issues.length+' operating model items to confirm'));const list=e('ul');a.issues.forEach(message=>{const li=e('li');const targets=issueTargets(message,model);if(targets.length){const link=e('a',message);link.href='#'+targets[0].id;link.onclick=event=>{event.preventDefault();document.dispatchEvent(new CustomEvent('bx-navigate',{detail:targets[0]}));};li.append(link);}else li.append(e('span',message));list.append(li);});review.append(list);root.append(review);
  const privatePanel=e('details',null,'bx-private-costings no-print');privatePanel.open=privateWasOpen;privatePanel.append(e('summary','Private provisional costing settings and cost review'));
  const settings=e('div',null,'bx-private-settings');window.BlocktexxPricing.renderSettings(settings,a);privatePanel.append(settings);
  const actuals=e('details');actuals.append(e('summary','View underlying operating costs'));const content=e('div');renderCosts(content,model);actuals.append(content);privatePanel.append(actuals);root.append(privatePanel);
 }
 return {calculate,render,renderCosts,issueTargets};
})();
