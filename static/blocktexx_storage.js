window.BlocktexxStorage=(()=>{
  const e=(t,text,c)=>{const n=document.createElement(t);if(text!=null)n.textContent=text;if(c)n.className=c;return n;};
  const num=n=>n==null?'Not set':Number(n).toLocaleString('en-AU',{maximumFractionDigits:2});
  const money=n=>n==null?'Not set':Number(n).toLocaleString('en-AU',{style:'currency',currency:'AUD'});
  const cents=n=>Math.round((n+Number.EPSILON)*100)/100;
  const dateText=s=>s?s.split('-').reverse().join('.'):'Set start date';
  const iso=d=>d.toISOString().slice(0,10);
  const addDay=d=>new Date(d.getTime()+86400000);
  const required=['total_containers','occupied_containers','monthly_rate','free_containers','current_pallets','new_pallets','repack_cost','containers_per_day','minimum_containers','lease_months'];
  function calculate(s){
    if(required.some(k=>s[k]==null||s[k]===''))return null;
    if(s.total_containers<s.occupied_containers||s.total_containers<s.minimum_containers||s.total_containers<s.free_containers||s.new_pallets<=0||s.current_pallets<=0||s.containers_per_day<=0||s.lease_months<=0)return null;
    const pallets=s.occupied_containers*s.current_pallets,needed=Math.ceil(pallets/s.new_pallets),commitment=Math.max(needed,s.minimum_containers);
    const cost=s.occupied_containers*s.repack_cost,workdays=Math.ceil(s.occupied_containers/s.containers_per_day);
    const scenario=(containers,capacity)=>{const billable=Math.max(0,containers-s.free_containers),rent=cents(billable*s.monthly_rate);return {containers,billable,rent,annual:rent*12,term:rent*s.lease_months,capacity:containers*capacity,spare:containers*capacity-pallets,perPallet:rent/(containers*capacity)};};
    const current=scenario(s.occupied_containers,s.current_pallets),compact=scenario(needed,s.new_pallets),lease=scenario(commitment,s.new_pallets);
    const recovery=s.recovery==='monthly'?cents(cost/s.lease_months):0,lastRecovery=s.recovery==='monthly'?cents(cost-recovery*(s.lease_months-1)):0,upfront=s.recovery==='upfront'?cost:0;
    const timeline=[];let finish=null,leaseStart=null;
    if(s.start_date&&workdays<=3660){
      let d=new Date(s.start_date+'T00:00:00Z'),done=0;
      while(done<workdays){
        const weekday=d.getUTCDay(),working=s.days_per_week===7||(weekday!==0&&(s.days_per_week===6||weekday!==6));
        if(working){done++;const processed=Math.min(s.occupied_containers,Math.floor(done*s.containers_per_day+1e-8));const output=Math.ceil(processed*s.current_pallets/s.new_pallets),occupied=s.occupied_containers-processed+output;
          timeline.push({date:iso(d),day:done,processed,output,occupied,released:s.occupied_containers-occupied,free:s.total_containers-occupied,spent:processed*s.repack_cost});}
        d=addDay(d);
      }
      finish=timeline.at(-1)?.date;leaseStart=iso(d);
    }
    return {pallets,needed,commitment,cost,workdays,current,compact,lease,recovery,lastRecovery,upfront,timeline,finish,leaseStart,
      released:s.occupied_containers-needed,free:s.total_containers-needed,uncommitted:s.total_containers-commitment,
      totalCapacity:s.total_containers*s.new_pallets,additionalCapacity:s.total_containers*(s.new_pallets-s.current_pallets),
      savings:current.rent-compact.rent,termReceipts:lease.term+upfront+(s.recovery==='monthly'?cost:0),
      retainedAfterRepack:lease.term+upfront+(s.recovery==='monthly'?cost:0)-cost};
  }
  function table(root,headers,rows){const wrap=e('div',null,'bx-scroll'),t=e('table',null,'bx-resource-table'),h=e('tr');headers.forEach(v=>h.append(e('th',v)));t.append(h);rows.forEach(values=>{const r=e('tr');values.forEach(v=>r.append(e('td',v)));t.append(r);});wrap.append(t);root.append(wrap);return t;}
  function metrics(root,items){const div=e('div',null,'bx-metrics');items.forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));div.append(card);});root.append(div);}
  function render(root,model,onChange){
    const s=model.storage||(model.storage={days_per_week:5,recovery:'absorbed',lease_months:36});
    const open=[...root.querySelectorAll('details[open]')].map(n=>n.dataset.section);
    root.replaceChildren(e('h2','Storage · capacity, repacking and lease analysis'),e('p','Separate storage cost centre. These figures do not enter collection or interstate cost/kg. All prices exclude GST.','bx-muted'));
    const inputs=e('div',null,'bx-settings');
    const fields=[['total_containers','Total containers available',1,10000,1],['occupied_containers','Currently occupied containers',1,10000,1],['monthly_rate','Monthly charge / container (ex GST)',0,100000,.0001],['free_containers','Free containers / month',0,10000,1],['current_pallets','Current pallets / container',1,10000,1],['new_pallets','Repacked pallets / container',1,10000,1],['repack_cost','Repack cost / source container',0,100000,.01],['containers_per_day','Source containers repacked / working day',.1,100,.1],['minimum_containers','Proposed minimum leased containers',0,10000,1],['lease_months','Lease term / months',1,120,1]];
    fields.forEach(([key,title,min,max,step])=>{const label=e('label',title),input=e('input');input.type='number';input.min=min;input.max=max;input.step=step;input.value=s[key]??'';input.setAttribute('aria-label',title);input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}s[key]=input.value===''?null:Number(input.value);onChange();};label.append(input);inputs.append(label);});
    const start=e('label','Repack start date'),date=e('input');date.type='date';date.value=s.start_date||'';date.setAttribute('aria-label','Repack start date');date.onchange=()=>{s.start_date=date.value;onChange();};start.append(date);inputs.append(start);
    const select=(key,label,options)=>{const l=e('label',label),v=e('select');v.setAttribute('aria-label',label);options.forEach(([value,title])=>{const o=e('option',title);o.value=value;v.append(o);});v.value=s[key]??(key==='days_per_week'?5:'absorbed');v.onchange=()=>{s[key]=key==='days_per_week'?Number(v.value):v.value;onChange();};l.append(v);inputs.append(l);};
    select('days_per_week','Working week',[[5,'5 days · Monday–Friday'],[6,'6 days · Monday–Saturday'],[7,'7 days · every day']]);
    select('recovery','Repacking cost recovery',[['absorbed','S&B funds repacking'],['upfront','BlockTexx pays upfront'],['monthly','Recover across the lease term']]);root.append(inputs);
    const save=e('button','Save storage model','primary');save.type='button';save.onclick=()=>document.getElementById('bx-save')?.click();root.append(save,e('p','Planning inputs: the container rate is treated as a monthly rental charge; the free allowance reduces billable containers. The minimum commitment is a scenario to negotiate. Existing stock is assumed to fill every current container to the entered pallet count.','bx-muted'));
    const a=calculate(s);if(!a){root.append(e('p','Enter all storage assumptions to calculate. Total available containers must cover occupied, minimum leased and free container quantities.','bx-warning'));return;}
    if(a.needed>s.total_containers){root.append(e('p','The repacked stock exceeds available container capacity. Increase capacity or review pallet assumptions.','bx-warning'));return;}
    if(s.minimum_containers<a.needed)root.append(e('p','Existing stock needs '+a.needed+' containers, so the effective lease quantity is '+a.commitment+' despite the lower minimum.','bx-warning'));
    metrics(root,[['Existing stock',num(a.pallets)+' pallets'],['Containers needed after repack',num(a.needed)],['Occupied containers released',num(a.released)],['Total empty containers afterwards',num(a.free)],['Repacking budget',money(a.cost)],['Repacking duration',num(a.workdays)+' working days']]);
    root.append(e('h3','Current storage versus repacked storage'));
    table(root,['Measure','Current loading','Repacked · minimum physical footprint','Repacked · proposed lease commitment'],[
      ['Pallets / container',num(s.current_pallets),num(s.new_pallets),num(s.new_pallets)],
      ['Containers leased',num(a.current.containers),num(a.compact.containers),num(a.lease.containers)],
      ['Billable after free allowance',num(a.current.billable),num(a.compact.billable),num(a.lease.billable)],
      ['Storage rent / month',money(a.current.rent),money(a.compact.rent),money(a.lease.rent)],
      ['Storage rent / year',money(a.current.annual),money(a.compact.annual),money(a.lease.annual)],
      ['Storage rent / '+s.lease_months+' months',money(a.current.term),money(a.compact.term),money(a.lease.term)],
      ['Contracted pallet capacity',num(a.current.capacity),num(a.compact.capacity),num(a.lease.capacity)],
      ['Room for additional pallets',num(a.current.spare),num(a.compact.spare),num(a.lease.spare)],
      ['Monthly rent / available pallet slot',money(a.current.perPallet),money(a.compact.perPallet),money(a.lease.perPallet)]
    ]);
    root.append(e('p','Capacity uses whole containers: stock ÷ repacked pallets per container, rounded up. Partly filled container space remains available. Cost per pallet slot uses contracted capacity, while current-stock cost is rent ÷ '+num(a.pallets)+' pallets.','bx-muted'));
    metrics(root,[['Entire site at new density',num(a.totalCapacity)+' pallet slots'],['Site capacity increase',num(a.additionalCapacity)+' slots · '+num((s.new_pallets/s.current_pallets-1)*100)+'%'],['Additional pallets within proposed lease',num(a.lease.spare)],['Containers outside proposed commitment',num(a.uncommitted)]]);
    root.append(e('h3','BlockTexx savings and S&B lease retention'));
    table(root,['Commercial outcome','Result'],[
      ['Footprint-only reduction · monthly rental saving for BlockTexx',money(a.savings)],
      ['Same reduction · monthly rental revenue lost by S&B',money(a.savings)],
      ['Proposed commitment · monthly rent change versus current',money(a.lease.rent-a.current.rent)],
      ['Proposed commitment · capacity change versus current',num((a.lease.capacity/a.current.capacity-1)*100)+'%'],
      ['Proposed commitment · cost per capacity slot reduction',num((1-a.lease.perPallet/a.current.perPallet)*100)+'%'],
      ['Footprint-only simple payback on repacking',a.savings>0?num(a.cost/a.savings)+' months of full post-repack rental savings':'No positive rental saving']
    ]);
    root.append(e('p','Retaining a minimum lease preserves reserved capacity and rental income while BlockTexx gains more pallet space per dollar. Empty reserved containers remain committed to BlockTexx. Pallet compatibility, safe loading and access requirements need operational confirmation.','bx-muted'));
    root.append(e('h3',s.lease_months+'-month lease and repacking recovery'));
    table(root,['Measure','Selected recovery approach'],[
      ['Regular monthly storage rent',money(a.lease.rent)],['Upfront repacking recovery',money(a.upfront)],
      ['Monthly repacking recovery',money(a.recovery)],['Final month repacking recovery',money(a.lastRecovery)],
      ['Normal monthly customer payment',money(a.lease.rent+a.recovery)],
      ['Total customer payments over lease',money(a.termReceipts)],
      ['S&B repacking expense',money(a.cost)],['S&B receipts after repacking expense',money(a.retainedAfterRepack)]
    ]);
    root.append(e('p','Receipts after repacking exclude all other operating, finance and storage costs. No rent escalation, discounting or GST is applied. The lease projection starts after repacking is complete; existing storage charges during repacking are separate. Rental invoices round to cents; the final recovery payment reconciles the exact repacking budget.','bx-muted'));
    const leaseDetails=e('details');leaseDetails.dataset.section='lease';leaseDetails.open=open.includes('lease');leaseDetails.append(e('summary','View lease payments by month'));
    table(leaseDetails,['Lease month','Storage rent','Repacking recovery','Customer payment','Cumulative receipts'],Array.from({length:s.lease_months},(_,i)=>{const recovery=i===s.lease_months-1?a.lastRecovery:a.recovery;return [i+1,money(a.lease.rent),money(recovery),money(a.lease.rent+recovery),money(a.upfront+a.lease.rent*(i+1)+(i===s.lease_months-1?(s.recovery==='monthly'?a.cost:0):a.recovery*(i+1)))];}));root.append(leaseDetails);
    root.append(e('h3','Repacking programme'));
    metrics(root,[['Working days',num(a.workdays)],['Working weeks',num(a.workdays/s.days_per_week)],['Forecast completion',dateText(a.finish)],['Proposed lease commencement',dateText(a.leaseStart)]]);
    root.append(e('p','Throughput counts original source containers unpacked and consolidated, not the smaller number of final storage containers. Consolidation is progressive, assumes unchanged inventory and requires suitable staging space. Calendar includes the selected workweek and excludes no public holidays or downtime. Lease commencement is modelled as the day after completion.','bx-muted'));
    if(!a.timeline.length){root.append(e('p','Set a start date to see dated milestones and monthly progress.','bx-warning'));return;}
    table(root,['Milestone','Date','Source containers repacked','Occupied after consolidation','Containers released','Cumulative repack cost'],[.25,.5,.75,1].map(f=>{const row=a.timeline.find(r=>r.processed>=Math.ceil(s.occupied_containers*f))||a.timeline.at(-1);return [num(f*100)+'%',dateText(row.date),num(row.processed),num(row.occupied),num(row.released),money(row.spent)];}));
    const byMonth=new Map();a.timeline.forEach(r=>byMonth.set(r.date.slice(0,7),r));
    table(root,['Month ending','Progress','Repacked source containers','Remaining source containers','New occupied containers','Total occupied','Total empty','Cost to date'],[...byMonth.values()].map(r=>{const progress=e('progress');progress.max=s.occupied_containers;progress.value=r.processed;progress.setAttribute('aria-label',num(r.processed/s.occupied_containers*100)+' percent repacked');return [dateText(r.date),num(r.processed/s.occupied_containers*100)+'%',num(r.processed),num(s.occupied_containers-r.processed),num(r.output),num(r.occupied),num(r.free),money(r.spent)];}));
    const daily=e('details');daily.dataset.section='daily';daily.open=open.includes('daily');daily.append(e('summary','View each working day'));table(daily,['Working day','Date','Repacked to date','Occupied containers','Released','Repack cost to date'],a.timeline.map(r=>[r.day,dateText(r.date),num(r.processed),num(r.occupied),num(r.released),money(r.spent)]));root.append(daily);
  }
  return {calculate,render};
})();
