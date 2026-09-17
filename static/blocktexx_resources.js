window.renderBlocktexxResources = function(model,state,onChange) {
  const root=document.getElementById('bx-resources');
  const wasOpen=root.querySelector('details')?.open||false;
  const labels={bin120:'120L bins',bin240:'240L bins',bin660:'660L bins',cage:'Cages',pallecon:'Pallecons'};
  const kinds=Object.keys(labels);
  const pricing=model.states[state].resource_pricing||(model.states[state].resource_pricing={});
  kinds.forEach(k=>pricing[k]=pricing[k]||{purchase_each:null,weekly_rent_each:null,rental_qty:null});
  const money=n=>n==null?'Not priced':new Intl.NumberFormat('en-AU',{style:'currency',currency:'AUD'}).format(n);
  function priceInput(kind,key,label,value,placeholder){
    const input=e('input');input.type='number';input.min='0';input.max='1000000';input.step=key==='rental_qty'?'1':'0.01';input.value=value??'';input.placeholder=placeholder||'Not set';input.setAttribute('aria-label',label);
    input.addEventListener('change',()=>{if(!input.checkValidity()){input.reportValidity();return;}pricing[kind][key]=input.value===''?null:Number(input.value);onChange();});return input;
  }
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const sites=model.sites.filter(s=>s.state===state);
  const total=Object.fromEntries(kinds.map(k=>[k,sites.reduce((n,s)=>n+(s.containers?.[k]||0),0)]));
  const unknown=sites.filter(s=>!kinds.some(k=>s.containers?.[k]));
  root.replaceChildren(e('h2',state+' · Resources'),
    e('p','Two of everything for one service cycle: one set at clients and one matching set for switch-outs. Each customer is counted once, regardless of collection frequency or how many runs visit them.'));
  const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table');
  const head=e('tr');['Container model','At clients · set 1','Switch-out stock · set 2','Total required','Purchase $ / each','Purchase total','Rental quantity','Rental $ / each / week','Weekly rental total'].forEach(t=>head.append(e('th',t)));table.append(head);
  let purchaseTotal=0,rentalTotal=0,rentalCount=0,purchaseMissing=0,rentalMissing=0;
  kinds.forEach(k=>{
    const profile=pricing[k],required=total[k]*2,qty=profile.rental_qty??required;
    const purchase=required===0?0:profile.purchase_each==null?null:required*profile.purchase_each;
    const rent=qty===0?0:profile.weekly_rent_each==null?null:qty*profile.weekly_rent_each;
    purchaseTotal+=purchase||0;rentalTotal+=rent||0;rentalCount+=qty;
    if(purchase==null)purchaseMissing++;if(rent==null)rentalMissing++;
    const row=e('tr');row.dataset.kind=k;
    [labels[k],total[k],total[k],required].forEach((v,i)=>row.append(e(i===0?'th':'td',v)));
    const cost=e('td');cost.append(priceInput(k,'purchase_each',labels[k]+' purchase cost each',profile.purchase_each));row.append(cost,e('td',money(purchase)));
    const count=e('td');count.append(priceInput(k,'rental_qty',labels[k]+' rental quantity',profile.rental_qty,String(required)));row.append(count);
    const rate=e('td');rate.append(priceInput(k,'weekly_rent_each',labels[k]+' weekly rental each',profile.weekly_rent_each));row.append(rate,e('td',money(rent)));table.append(row);
  });
  const sum=Object.values(total).reduce((n,v)=>n+v,0),foot=e('tr');foot.className='bx-resource-total';
  ['Summary',sum,sum,sum*2,'—',money(purchaseTotal)+(purchaseMissing||unknown.length?' known subtotal':''),rentalCount,'—',money(rentalTotal)+(rentalMissing||unknown.length?' known subtotal':'')].forEach((v,i)=>foot.append(e(i===0?'th':'td',v)));
  table.append(foot);wrap.append(table);root.append(e('p','AUD, excluding GST. Purchase total = total required × unit purchase cost. Weekly rental = rental quantity × weekly rate. Blank rental quantity uses the two-set requirement automatically; enter a quantity to explore partial rental.'),wrap);
  const summary=e('div',null,'bx-metrics');
  [['Purchase cost',money(purchaseTotal)],['Rental / week',money(rentalTotal)],['Rental / average month',money(rentalTotal*52/12)],['Rental / year',money(rentalTotal*52)]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));summary.append(card);});root.append(summary);
  if(purchaseMissing||rentalMissing||unknown.length)root.append(e('p','Scenario incomplete: '+purchaseMissing+' model purchase prices and '+rentalMissing+' model rental rates are missing for required quantities'+(unknown.length?'; some customer quantities are also unknown':'')+'. Summary amounts include priced, known quantities only.','bx-warning'));
  root.append(e('p','Purchase and rental are separate scenarios. These amounts are not added to the collection cost/kg. Rental projections assume 52 charged weeks per year and exclude delivery, damage and other fees.','bx-muted'));
  root.append(e('p','Includes every customer in this state’s register, including ad hoc and paused customers, so their stock is reserved. These are required stock quantities, not confirmed stock on hand. Extra spares and stock held for longer processing times are not added.','bx-muted'));
  if(unknown.length)root.append(e('p','Incomplete: '+unknown.length+' customers have no confirmed container quantities: '+unknown.map(s=>s.name).join('; ')+'. Totals show known quantities only.','bx-warning'));
  root.append(e('p','Based on the current customer quantities imported from the spreadsheet. Review duplicated source equipment rows before purchasing.','bx-muted'));
  const detail=e('details');detail.open=wasOpen;detail.append(e('summary','View / edit the customer quantities behind these totals'));
  const scroll=e('div',null,'bx-scroll'),customers=e('table',null,'bx-resource-table'),header=e('tr');
  ['Customer',...kinds.map(k=>labels[k])].forEach(t=>header.append(e('th',t)));customers.append(header);
  sites.forEach(s=>{const row=e('tr');row.append(e('th',s.name));
    kinds.forEach(k=>{const td=e('td'),input=e('input');input.type='number';input.min='0';input.max='500';input.step='1';input.value=s.containers?.[k]??0;input.setAttribute('aria-label',s.name+' resource '+labels[k]);
      input.addEventListener('change',()=>{if(input.value===''||!input.checkValidity()){input.value=s.containers?.[k]??0;input.reportValidity();return;}s.containers=s.containers||{};s.containers[k]=Number(input.value);onChange();});td.append(input);row.append(td);});customers.append(row);});
  scroll.append(customers);detail.append(scroll);root.append(detail,e('p','Quantity edits update resources and truck load plans. Select Save model to keep quantities and price profiles.','bx-muted'));
};
