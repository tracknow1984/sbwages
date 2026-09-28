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
  const names=['NSW','BANYO','BAGS'];
  const range=(low,high)=>low===high?num(low):num(low)+'–'+num(high);
  function sectionSummary(s){
    const rows=names.map(name=>({name,...s.sections?.[name]}));
    const assigned=rows.reduce((n,r)=>n+(r.filled??0),0),balanced=s.occupied_containers!=null&&rows.every(r=>r.filled!=null)&&assigned===s.occupied_containers;
    const complete=balanced&&s.total_containers!=null&&s.total_containers>=assigned&&rows.every(r=>r.filled===0||(r.pallets_min>0&&r.pallets_max>=r.pallets_min&&r.target>0));
    if(!complete)return {complete:false,assigned,remaining:s.occupied_containers==null?null:s.occupied_containers-assigned,rows};
    const sections=rows.map(r=>{const low=r.filled*(r.pallets_min||0),high=r.filled*(r.pallets_max||0),needLow=r.filled?Math.ceil(low/r.target):0,needHigh=r.filled?Math.ceil(high/r.target):0;return {...r,low,high,needLow,needHigh,releaseLow:r.filled-needHigh,releaseHigh:r.filled-needLow};});
    const needLow=sections.reduce((n,r)=>n+r.needLow,0),needHigh=sections.reduce((n,r)=>n+r.needHigh,0),freeLow=s.total_containers-needHigh,freeHigh=s.total_containers-needLow;
    const availability=sections.map(r=>({name:r.name,low:r.target>0?freeLow*r.target+r.needHigh*r.target-r.high:null,high:r.target>0?freeHigh*r.target+r.needLow*r.target-r.low:null}));
    return {complete:true,assigned,remaining:0,sections,needLow,needHigh,freeLow,freeHigh,releaseLow:assigned-needHigh,releaseHigh:assigned-needLow,initialFree:s.total_containers-assigned,availability};
  }
  function financialSummary(s){
    const rate=s.monthly_rate;
    if(rate==null||!Number.isFinite(rate)||rate<0)return null;
    const a=sectionSummary(s);
    const sections=names.map(name=>{
      const r=s.sections?.[name]||{},valid=r.pallets_min>0&&r.pallets_max>=r.pallets_min&&r.target>0;
      const currentLow=valid?rate/r.pallets_max:null,currentHigh=valid?rate/r.pallets_min:null,after=valid?rate/r.target:null;
      const capacity=a.complete?a.sections.find(row=>row.name===name):null;
      return {name,currentLow,currentHigh,after,savingLow:valid?currentLow-after:null,savingHigh:valid?currentHigh-after:null,
        valueLow:capacity?capacity.releaseLow*rate:null,valueHigh:capacity?capacity.releaseHigh*rate:null,
        repack:r.filled!=null&&s.repack_cost!=null?r.filled*s.repack_cost:null};
    });
    return {sections,valueLow:a.complete?a.releaseLow*rate:null,valueHigh:a.complete?a.releaseHigh*rate:null,
      repack:s.occupied_containers!=null&&s.repack_cost!=null?s.occupied_containers*s.repack_cost:null};
  }
  function renderFinancials(root,s){
    root.append(e('h3','Storage cost per pallet · monthly, ex GST'));
    const f=financialSummary(s);
    if(!f){root.append(e('p','Enter the contract rate to compare pallet costs.','bx-muted'));return;}
    const cashRange=(low,high)=>low==null?'Complete assumptions':low===high?money(low):money(low)+'–'+money(high);
    table(root,['Section','Current cost / pallet','After repack / pallet','Saving / pallet'],f.sections.map(r=>[r.name,cashRange(r.currentLow,r.currentHigh),r.after==null?'Complete assumptions':money(r.after),cashRange(r.savingLow,r.savingHigh)]));
    root.append(e('p','Contract rate ÷ pallets per container. Current costs use the minimum–maximum pallet range; after-repack costs assume the new pallet capacity is fully used. Rates are before the shared free-container allowance. These are unit-cost savings as capacity is used; the retained monthly bill stays the same.','bx-muted'));
    root.append(e('h3','Dollar value of space freed by repacking'));
    table(root,['Section','Capacity value / month','Capacity value / year','One-off repack cost'],f.sections.map(r=>[r.name,cashRange(r.valueLow,r.valueHigh),cashRange(r.valueLow==null?null:r.valueLow*12,r.valueHigh==null?null:r.valueHigh*12),money(r.repack)]).concat([['Total',cashRange(f.valueLow,f.valueHigh),cashRange(f.valueLow==null?null:f.valueLow*12,f.valueHigh==null?null:f.valueHigh*12),money(f.repack)]]));
    root.append(e('p','Capacity value = whole containers released × the contract rate. It values additional space within the existing lease, not a cash saving or a reduction in rent. Annual values assume that space is available for a full year after repacking. Existing empty containers are excluded. Repack costs use one labour day per source container.','bx-muted'));
  }
  function render(root,model,onChange){
    const s=model.storage||(model.storage={days_per_week:5,recovery:'absorbed'});
    if(s.total_containers!=null)s.minimum_containers=s.total_containers;
    s.containers_per_day=1;
    s.sections=s.sections||{};names.forEach(name=>s.sections[name]=s.sections[name]||{});
    const a=sectionSummary(s),wasOpen=root.querySelector('details[data-settings]')?.open;
    root.replaceChildren(e('h2','Storage · retained contract and available space'),e('p','All containers stay in the storage contract. Repacking consolidates existing stock and releases space for more stock.','bx-muted'));
    metrics(root,[['Containers retained under contract',num(s.total_containers)],['Currently full',num(s.occupied_containers)],['Currently empty',s.total_containers!=null&&s.occupied_containers!=null?num(s.total_containers-s.occupied_containers):'Not set'],['Section allocation',num(a.assigned)+' / '+num(s.occupied_containers)]]);
    const rentCard=root.querySelector('.bx-metrics > div');
    if(s.total_containers!=null&&s.monthly_rate!=null){
      rentCard.append(e('p',money(cents(s.total_containers*s.monthly_rate))+' / month ex GST before allowance','bx-muted'));
      if(s.free_containers!=null){
        rentCard.append(e('p',money(cents(Math.max(0,s.total_containers-s.free_containers)*s.monthly_rate))+' / month ex GST payable after '+num(s.free_containers)+' free containers','bx-muted'));
      }
    }else{
      rentCard.append(e('p','Set container count and contract rate for monthly cost.','bx-muted'));
    }
    const settings=e('details');settings.dataset.settings='true';settings.open=wasOpen??!a.complete;settings.append(e('summary','Edit storage assumptions'));
    const inputs=e('div',null,'bx-settings');
    const field=(root,obj,key,title,min,max,step=1)=>{const label=e('label',title),input=e('input');input.type='number';input.min=min;input.max=max;input.step=step;input.value=obj[key]??'';input.setAttribute('aria-label',title);input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}obj[key]=input.value===''?null:Number(input.value);onChange();};label.append(input);root.append(label);};
    field(inputs,s,'total_containers','Total containers retained under contract',1,10000);
    field(inputs,s,'occupied_containers','Total full containers',1,10000);
    field(inputs,s,'repack_cost','Repacking cost per day including labour',0,100000,.01);
    field(inputs,s,'monthly_rate','Contract rate per container per month (ex GST)',0,100000,.0001);
    field(inputs,s,'free_containers','Monthly free-container allowance',0,10000);
    settings.append(inputs,e('p','One working day to repack one source container. The monthly allowance reduces billing only; all contracted containers remain available.','bx-muted'));
    names.forEach(name=>{const group=e('section',null,'bx-storage-section');group.append(e('h3',name));const grid=e('div',null,'bx-settings');field(grid,s.sections[name],'filled',name+' full containers',0,10000);field(grid,s.sections[name],'pallets_min',name+' pallet range — minimum',1,10000);field(grid,s.sections[name],'pallets_max',name+' pallet range — maximum',1,10000);field(grid,s.sections[name],'target',name+' pallets after repack',1,10000);group.append(grid);settings.append(group);});root.append(settings);
    const save=e('button','Save storage assumptions','primary');save.type='button';save.disabled=!a.complete;save.onclick=()=>document.getElementById('bx-save')?.click();root.append(save);
    if(!a.complete)root.append(e('p',a.remaining==null?'Enter the overall container numbers and section breakdown.':a.remaining<0?'Section counts exceed the full-container total by '+num(-a.remaining)+'.':a.remaining>0?num(a.remaining)+' full containers still need a section. Enter NSW, BANYO and BAGS counts; they must match the total.':'Complete each section’s pallet range and repacked capacity.','bx-warning'));
    table(root,['Section','Full containers','Current pallets / container','After repacking'],names.map(name=>{const r=s.sections[name];return [name,num(r.filled),r.pallets_min!=null&&r.pallets_max!=null?range(r.pallets_min,r.pallets_max):'Not set',num(r.target)];}));
    if(s.occupied_containers!=null){
      root.append(e('h3','Repacking cost and duration'));
      metrics(root,[['Source containers to repack',num(s.occupied_containers)],['Labour days · one per container',num(s.occupied_containers)],['Total repacking cost',s.repack_cost!=null?money(s.occupied_containers*s.repack_cost):'Set daily cost'],['At 5–6 containers per week',range(Math.ceil(s.occupied_containers/6),Math.ceil(s.occupied_containers/5))+' weeks']]);
    }
    if(s.total_containers!=null&&s.monthly_rate!=null&&s.free_containers!=null){
      const billed=Math.max(0,s.total_containers-s.free_containers);root.append(e('p','Retained contract: '+num(s.total_containers)+' containers · '+num(billed)+' billed after '+num(s.free_containers)+' free · '+money(cents(billed*s.monthly_rate))+' per month ex GST. Repacking does not reduce the contracted container count.','bx-muted'));
    }
    renderFinancials(root,s);
    if(!a.complete)return;
    root.append(e('h3','Container breakdown after repacking'));
    table(root,['Section','Current full','Full after repacking','Containers released'],a.sections.map(r=>[r.name,num(r.filled),range(r.needLow,r.needHigh),range(r.releaseLow,r.releaseHigh)]).concat([['Total',num(s.occupied_containers),range(a.needLow,a.needHigh),range(a.releaseLow,a.releaseHigh)]]));
    metrics(root,[['Containers still contracted',num(s.total_containers)],['Existing stock after repacking',range(a.needLow,a.needHigh)+' containers'],['Empty containers available afterwards',range(a.freeLow,a.freeHigh)],['Extra empty containers created',range(a.releaseLow,a.releaseHigh)]]);
    if(a.freeLow<0)root.append(e('p','At the high end of current stock, there is insufficient contracted capacity. Review the section pallet assumptions.','bx-warning'));
    root.append(e('h3','How much additional stock can fit?'));
    table(root,['If empty containers are used for…','Additional pallet spaces available'],a.availability.map(r=>[r.name,r.low==null?'Set repacked capacity':range(r.low,r.high)]));
    root.append(e('p','These are alternative uses of the same empty containers, not amounts to add together. Each includes spare pallet space in that section’s partially filled containers. A future mix can be allocated between sections.','bx-muted'));
    root.append(e('p','Low–high estimates use the current pallet ranges. Containers are rounded up separately for NSW, BANYO and BAGS; stock is not mixed between sections. The lower availability figure is the conservative case. Repacking assumes unchanged stock and suitable staging space.','bx-muted'));
  }
  return {calculate,capacitySummary,sectionSummary,financialSummary,render};
})();
