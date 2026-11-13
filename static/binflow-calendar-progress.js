'use strict';
(()=>{
 const root=document.querySelector('[data-progress-url]');if(!root)return;
 const labels={booked:'Booked',on_route:'On route',completed:'Completed',cancelled:'Cancelled'};
 const symbols={booked:'○',on_route:'➜',completed:'✓',cancelled:'×'};
 const updated=root.querySelector('[data-progress-updated]');
 let data={},busy=false;
 const node=(tag,text,css)=>{const n=document.createElement(tag);n.textContent=text;if(css)n.className=css;return n;};
 function draw(){
  const opened=new Set([...root.querySelectorAll('[data-progress-key][open]')].map(n=>n.dataset.progressKey));
  root.querySelectorAll('[data-progress-key]').forEach(n=>n.remove());
  if(root.hasAttribute('data-calendar-editor'))root.querySelectorAll('[data-closed-state]').forEach(n=>n.remove());
  for(const [key,day] of Object.entries(data)){
   const cell=root.querySelector('td[data-date="'+day.date+'"] .bf-date-events');if(!cell)continue;
   if(day.closed && root.hasAttribute('data-calendar-editor')){
    const lock=node('div','','bf-note');lock.dataset.closedState=day.state;
    const link=node('a','Completed PODs ('+day.closed.pods+')');
    link.href='/binflow/pods?'+new URLSearchParams({view:'signed',state:day.state,date:day.date});
    lock.append(node('strong',day.state+' · Day locked'),document.createElement('br'),link);
    cell.before(lock);
    cell.querySelectorAll('[data-drag-state="'+day.state+'"]').forEach(n=>n.remove());
   }
   const details=node('details','','bf-calendar-progress');details.dataset.progressKey=key;details.open=opened.has(key);
   const summary=node('summary','');
   summary.append(node('span',(root.dataset.state?'':day.state+' · ')+day.entries.length+' pickups','bf-progress-total'));
   const counts=node('span','','bf-progress-counts');
   for(const status of Object.keys(labels)){
    const count=day.counts[status],badge=node('span',symbols[status]+' '+count,'bf-progress-chip is-'+status+(count?'':' is-zero'));
    badge.title=count+' '+labels[status].toLowerCase();badge.setAttribute('aria-label',badge.title);counts.append(badge);
   }
   summary.append(counts);details.append(summary);
   const list=node('ul','');
   for(const entry of day.entries){
    const row=node('li',''),icon=node('span',symbols[entry.status],'bf-progress-chip is-'+entry.status);
    icon.title=labels[entry.status];icon.setAttribute('aria-label',labels[entry.status]);row.append(icon);
    const link=entry.job_id && !root.hasAttribute('data-calendar-readonly');
    const name=node(link?'a':'span',entry.title);name.title=labels[entry.status]+' · '+entry.note;
    if(link)name.href='/binflow/pickups/'+entry.job_id;
    name.dataset.noIcon='';row.append(name);list.append(row);
   }
   details.append(list);
   const card=[...cell.querySelectorAll('.bf-cal-event')].find(n=>n.dataset.state===day.state);
   if(card)card.after(details);else cell.append(details);
  }
 }
 async function refresh(){
  if(busy||document.hidden)return;busy=true;
  try{
   const url=new URL(root.dataset.progressUrl,location.origin);
   for(const key of ['first','last','state'])url.searchParams.set(key,root.dataset[key]||'');
   const response=await fetch(url,{headers:{Accept:'application/json'},cache:'no-store'});
   if(!response.ok)throw Error();data=(await response.json()).days;draw();
   updated.textContent='Live · '+new Date().toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit'});
  }catch(_){updated.textContent='Progress unavailable — retrying';}
  finally{busy=false;}
 }
 root.addEventListener('calendar-progress-redraw',draw);
 document.addEventListener('visibilitychange',refresh);window.addEventListener('focus',refresh);
 setInterval(refresh,20000);refresh();
})();
