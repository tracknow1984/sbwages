window.renderBlocktexxResources = function(model,state,onChange) {
  const root=document.getElementById('bx-resources');
  const wasOpen=root.querySelector('details')?.open||false;
  const labels={bin120:'120L bins',bin240:'240L bins',bin660:'660L bins',cage:'Cages',pallecon:'Pallecons'};
  const kinds=Object.keys(labels);
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const sites=model.sites.filter(s=>s.state===state);
  const total=Object.fromEntries(kinds.map(k=>[k,sites.reduce((n,s)=>n+(s.containers?.[k]||0),0)]));
  const unknown=sites.filter(s=>!kinds.some(k=>s.containers?.[k]));
  root.replaceChildren(e('h2',state+' · Resources'),
    e('p','Two of everything for one service cycle: one set at clients and one matching set for switch-outs. Each customer is counted once, regardless of collection frequency or how many runs visit them.'));
  const wrap=e('div',null,'bx-scroll'),table=e('table',null,'bx-resource-table');
  const head=e('tr');['Container model','At clients · set 1','Switch-out stock · set 2','Total required'].forEach(t=>head.append(e('th',t)));table.append(head);
  kinds.forEach(k=>{const row=e('tr');row.dataset.kind=k;[labels[k],total[k],total[k],total[k]*2].forEach((v,i)=>row.append(e(i===0?'th':'td',v)));table.append(row);});
  const sum=Object.values(total).reduce((n,v)=>n+v,0),foot=e('tr');foot.className='bx-resource-total';
  ['All containers',sum,sum,sum*2].forEach((v,i)=>foot.append(e(i===0?'th':'td',v)));table.append(foot);wrap.append(table);root.append(wrap);
  root.append(e('p','Includes every customer in this state’s register, including ad hoc and paused customers, so their stock is reserved. These are required stock quantities, not confirmed stock on hand. Extra spares and stock held for longer processing times are not added.','bx-muted'));
  if(unknown.length)root.append(e('p','Incomplete: '+unknown.length+' customers have no confirmed container quantities: '+unknown.map(s=>s.name).join('; ')+'. Totals show known quantities only.','bx-warning'));
  root.append(e('p','Based on the current customer quantities imported from the spreadsheet. Review duplicated source equipment rows before purchasing.','bx-muted'));
  const detail=e('details');detail.open=wasOpen;detail.append(e('summary','View / edit the customer quantities behind these totals'));
  const scroll=e('div',null,'bx-scroll'),customers=e('table',null,'bx-resource-table'),header=e('tr');
  ['Customer',...kinds.map(k=>labels[k])].forEach(t=>header.append(e('th',t)));customers.append(header);
  sites.forEach(s=>{const row=e('tr');row.append(e('th',s.name));
    kinds.forEach(k=>{const td=e('td'),input=e('input');input.type='number';input.min='0';input.max='500';input.step='1';input.value=s.containers?.[k]??0;input.setAttribute('aria-label',s.name+' resource '+labels[k]);
      input.addEventListener('change',()=>{if(input.value===''||!input.checkValidity()){input.value=s.containers?.[k]??0;input.reportValidity();return;}s.containers=s.containers||{};s.containers[k]=Number(input.value);onChange();});td.append(input);row.append(td);});customers.append(row);});
  scroll.append(customers);detail.append(scroll);root.append(detail,e('p','Quantity edits update resources and truck load plans. Select Save model to keep them.','bx-muted'));
};
