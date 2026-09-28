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
  function capacitySummary(s){
    const keys=['occupied_containers','current_pallets','new_pallets','minimum_containers','repack_cost'];
    if(keys.some(k=>s[k]==null||s[k]==='')||s.occupied_containers<=0||s.current_pallets<=0||s.new_pallets<=0||s.minimum_containers<=0||s.repack_cost<0)return null;
    const stock=s.occupied_containers*s.current_pallets,capacity=s.minimum_containers*s.new_pallets,needed=Math.ceil(stock/s.new_pallets);
    return {stock,capacity,needed,spare:capacity-stock,empty:s.minimum_containers-needed,cost:s.occupied_containers*s.repack_cost,
      weeks5:Math.ceil(s.occupied_containers/5),weeks6:Math.ceil(s.occupied_containers/6),increase:(capacity/stock-1)*100};
  }
  function render(root,model,onChange){
    const s=model.storage||(model.storage={days_per_week:5,recovery:'absorbed'});
    root.replaceChildren(e('h2','Storage · more capacity within the minimum lease'),e('p','Repack the existing stock and show the room available for growth. All costs exclude GST.','bx-muted'));
    const inputs=e('div',null,'bx-settings');
    [['occupied_containers','Current containers to repack',1,10000,1],['current_pallets','Current pallets per container',1,10000,1],['new_pallets','Pallets per container after repacking',1,10000,1],['minimum_containers','Minimum containers retained on lease',1,10000,1],['repack_cost','Repacking cost per source container',0,100000,.01]].forEach(([key,title,min,max,step])=>{
      const label=e('label',title),input=e('input');input.type='number';input.min=min;input.max=max;input.step=step;input.value=s[key]??'';input.setAttribute('aria-label',title);
      input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}s[key]=input.value===''?null:Number(input.value);onChange();};label.append(input);inputs.append(label);
    });root.append(inputs);
    const save=e('button','Save storage model','primary');save.type='button';save.onclick=()=>document.getElementById('bx-save')?.click();root.append(save);
    const a=capacitySummary(s);if(!a){root.append(e('p','Enter the five assumptions above to see capacity, repacking cost and duration.','bx-muted'));return;}
    metrics(root,[['Minimum containers on lease',num(s.minimum_containers)],['Capacity after repacking',num(a.capacity)+' pallets'],['Additional room for growth',num(a.spare)+' pallets'],['Total repacking cost',money(a.cost)]]);
    if(a.spare<0)root.append(e('p','The proposed minimum cannot hold the current stock. At least '+num(a.needed)+' containers are required at the new loading rate.','bx-warning'));
    table(root,['Storage capacity','Pallets'],[['Current stock · '+num(s.occupied_containers)+' containers × '+num(s.current_pallets),num(a.stock)],['After repacking · '+num(s.minimum_containers)+' leased containers × '+num(s.new_pallets),num(a.capacity)],['Available for additional stock',num(a.spare)+' · '+num(a.increase)+'% above current stock']]);
    root.append(e('p','Existing stock will occupy '+num(a.needed)+' containers after repacking, leaving '+num(Math.max(0,a.empty))+' empty containers within the minimum lease, plus any spare space in the partly filled container.','bx-muted'));
    root.append(e('h3','How long will repacking take?'));
    table(root,['Repacking rate','Time to repack all '+num(s.occupied_containers)+' source containers'],[['5 containers per week',num(a.weeks5)+' weeks'],['6 containers per week',num(a.weeks6)+' weeks']]);
    const timeline=e('div',null,'bx-metrics');[['5 per week',a.weeks5],['6 per week',a.weeks6]].forEach(([title,weeks])=>{const card=e('div',title),bar=e('progress');bar.max=a.weeks5;bar.value=weeks;bar.setAttribute('aria-label',title+' · '+weeks+' weeks');card.append(e('strong',weeks+' weeks'),bar);timeline.append(card);});root.append(timeline);
    root.append(e('p','Repacking cost = '+num(s.occupied_containers)+' source containers × '+money(s.repack_cost)+'. The estimate includes the entered unpacking, labour and forklift allowance. Duration is rounded up to complete weeks and assumes a steady rate with no interruptions or new incoming stock.','bx-muted'));
  }
  return {calculate,capacitySummary,render};
})();
