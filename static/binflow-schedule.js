'use strict';
(() => {
  const root = document.querySelector('.bf-week-plan');
  if (!root) return;
  if (root.dataset.driver === 'yes') {
    setInterval(async () => {
      if (document.hidden) return;
      try {
        const response = await fetch(root.dataset.versionUrl, {headers: {Accept: 'application/json'}});
        if (!response.ok) return;
        const data = await response.json();
        if (String(data.revision) !== root.dataset.revision) location.reload();
      } catch (_) { /* Keep the last readable schedule during a connection interruption. */ }
    }, 20000);
  }
  if (root.dataset.admin !== 'yes') return;
  const data = JSON.parse(document.getElementById('week-editor-data').textContent);
  const plan = data.plan, grid = root.querySelector('[data-week-grid]');
  const message = document.getElementById('week-save-message');
  const approval = document.getElementById('week-approval'), add = document.getElementById('week-add');
  const names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  let dirty = false, saving = false, dragged = null, addDay = 0;
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action, className = 'secondary') => {
    const node = el('button', text, className); node.type = 'button'; node.addEventListener('click', action); return node;
  };
  const mark = () => {
    dirty = true;
    message.textContent = 'Unsaved changes. Save to apply moves and update the driver if this week is published.';
  };
  const options = (value, onChange, label) => {
    const select = el('select'); select.setAttribute('aria-label', label);
    plan.days.forEach((d,i) => { const option = el('option', names[i]+' · '+d.date.slice(5)); option.value=i; select.append(option); });
    select.value = value;
    select.addEventListener('change', () => onChange(Number(select.value))); return select;
  };
  function move(source, index, target, before) {
    if (source === target && before === index) return;
    const [item] = plan.days[source].items.splice(index, 1);
    if (!item) return;
    const dest = plan.days[target].items;
    let position = before === undefined ? dest.length : before;
    if (source === target && before > index) position--;
    dest.splice(position, 0, item);
    plan.days[source].estimate_valid = false; plan.days[target].estimate_valid = false;
    mark(); draw();
  }
  function swap(source, target) {
    if (source === target) return;
    const a = plan.days[source], b = plan.days[target];
    [a.items,b.items] = [b.items,a.items]; [a.notes,b.notes] = [b.notes,a.notes];
    a.estimate_valid = b.estimate_valid = false;
    mark(); draw();
  }
  function draw() {
    grid.replaceChildren();
    plan.days.forEach((day, index) => {
      const section = el('section', undefined, 'bf-week-day'); section.dataset.day = index;
      const heading = el('header', undefined, 'bf-week-day-head'); heading.draggable = true;
      heading.append(el('h3',names[index]), el('time',day.date));
      heading.title = 'Drag to another day to swap the whole day';
      heading.addEventListener('dragstart', event => { dragged={day:index}; event.dataTransfer.setData('text/plain',names[index]); event.dataTransfer.effectAllowed='move'; });
      section.append(heading);
      const dayMove = el('label','Swap day with'); dayMove.append(options(index,target=>swap(index,target),'Swap '+names[index]+' with another day')); section.append(dayMove);
      if (day.estimate_valid && day.estimate_minutes) section.append(el('small',(day.estimate_minutes/60).toFixed(1)+'h original route estimate'));
      else if (day.items.length) section.append(el('small','Route and hours need review','bf-week-warning'));
      section.addEventListener('dragover', event => { if (dragged) {event.preventDefault();section.classList.add('bf-drop-target');} });
      section.addEventListener('dragleave', event => { if (!section.contains(event.relatedTarget)) section.classList.remove('bf-drop-target'); });
      section.addEventListener('drop', event => {
        event.preventDefault();section.classList.remove('bf-drop-target');
        if (!dragged) return;
        if (dragged.item === undefined) swap(dragged.day,index);
        else {
          const target = event.target.closest('[data-item-index]');
          move(dragged.day,dragged.item,index,target ? Number(target.dataset.itemIndex) : undefined);
        }
        dragged=null;
      });
      if(day.estimate_valid && day.loads?.length) {
        const loads=el('details',undefined,'bf-week-loads');loads.append(el('summary','Depot loads · '+day.loads.length));
        day.loads.forEach((load,n)=>{loads.append(el('p','Load '+(n+1)+' · '+load.start+'–'+load.end));const list=el('ul');
          load.stops.forEach(stop=>list.append(el('li',stop.title+' · '+stop.large+' × 1100L / '+stop.small+' × 240L')));loads.append(list,el('p','Return to depot after this load.'));});
        section.append(loads);
      }
      day.items.forEach((item, n) => {
        const card = el('article',undefined,'bf-week-stop'); card.draggable=true;card.dataset.itemIndex=n;
        card.addEventListener('dragstart', event => { event.stopPropagation();dragged={day:index,item:n};event.dataTransfer.setData('text/plain',item.id);event.dataTransfer.effectAllowed='move'; });
        card.append(el('small', 'Stop '+(n+1)+' · '+(item.kind==='job'?'Job / allocation':item.kind==='visit'?'Client visit':'Depot')),
          el('strong',item.title),el('p',item.address),el('p',item.detail));
        if (item.status) card.append(el('small',item.status));
        const timeLabel=el('label','Arrival (optional)'), time=el('input');time.type='time';time.min='06:30';time.max='15:30';time.value=item.time||'';
        time.addEventListener('change',()=>{item.time=time.value;mark();});timeLabel.append(time);card.append(timeLabel);
        const notesLabel=el('label','Stop notes'), notes=el('textarea');notes.rows=2;notes.maxLength=1000;notes.value=item.notes||'';
        notes.addEventListener('input',()=>{item.notes=notes.value;mark();});notesLabel.append(notes);card.append(notesLabel);
        const moveLabel=el('label','Move to day');moveLabel.append(options(index,target=>move(index,n,target),'Move '+item.title+' to day'));card.append(moveLabel);
        const controls=el('div',undefined,'bf-week-stop-actions');
        const up=button('↑',()=>move(index,n,index,n-1));up.setAttribute('aria-label','Move '+item.title+' earlier');up.disabled=n===0;
        const down=button('↓',()=>move(index,n,index,n+2));down.setAttribute('aria-label','Move '+item.title+' later');down.disabled=n===day.items.length-1;
        controls.append(up,down,button('Remove',()=>{day.items.splice(n,1);day.estimate_valid=false;mark();draw();}));card.append(controls);
        if(item.kind==='job') {const link=el('a','Open allocation →');link.href='/binflow/pickups/'+item.job_id;card.append(link);}
        section.append(card);
      });
      if (!day.items.length) section.append(el('p','No work scheduled','bf-muted'));
      section.append(button('+ Add job / allocation',()=>openAdd(index)));
      const noteLabel=el('label','Day instructions'),note=el('textarea');note.rows=2;note.maxLength=1000;note.value=day.notes||'';
      note.addEventListener('input',()=>{day.notes=note.value;mark();});noteLabel.append(note);section.append(noteLabel);
      section.append(button('Save day changes',()=>save('save'),'primary'));grid.append(section);
    });
  }
  const kind=add.querySelector('[data-add-kind]'), choice=add.querySelector('[data-add-choice]');
  function populateChoices() {
    choice.replaceChildren();
    const used=new Set(plan.days.flatMap(d=>d.items).filter(i=>i.kind==='job').map(i=>i.job_id));
    const source=kind.value==='job'?data.jobs.filter(i=>!used.has(i.job_id)):data.sites;
    for (const item of source) {const option=el('option',item.title+' · '+item.detail);option.value=kind.value==='job'?item.job_id:item.profile_id;choice.append(option);}
    add.querySelector('[data-add-choice-label]').hidden=kind.value==='baling';
    add.querySelector('[data-add-message]').textContent=kind.value==='job'&&!source.length?'No available jobs / allocations. Add a client visit, or create a new bin allocation first.':kind.value==='job'?'Adding a job replaces a placeholder visit for the same site on this day.':'';
    add.querySelector('[data-add-confirm]').disabled=kind.value!=='baling'&&!source.length;
  }
  function openAdd(index) { addDay=index;add.querySelector('[data-add-date]').textContent=names[index]+' · '+plan.days[index].date;populateChoices();add.showModal(); }
  kind.addEventListener('change',populateChoices);
  add.querySelector('[data-add-confirm]').addEventListener('click',()=>{
    let item;
    if(kind.value==='baling') item={kind:'baling',title:'Baling & depot work',address:'',detail:'Emptying, bin preparation and stock checks',time:'06:30',notes:''};
    else {
      const source=kind.value==='job'?data.jobs:data.sites;
      const selected=source.find(i=>String(kind.value==='job'?i.job_id:i.profile_id)===choice.value);
      if(!selected)return;item=structuredClone(selected);
    }
    item.id='new-'+crypto.randomUUID();
    if(item.kind==='job') plan.days[addDay].items=plan.days[addDay].items.filter(i=>i.kind!=='visit'||i.site_id!==item.site_id);
    plan.days[addDay].items.push(item);plan.days[addDay].estimate_valid=false;mark();draw();add.close();
  });
  async function save(action) {
    if(saving)return;
    const error=approval.querySelector('[data-approval-error]');error.textContent='';
    if(action==='approve'&&(!approval.querySelector('[data-week-driver]').value||!approval.querySelector('[data-week-reviewed]').checked)) {
      error.textContent='Select the driver and confirm the dispatch checks.';return;
    }
    const form=new FormData();form.set('csrf',document.getElementById('week-csrf').value);form.set('state',root.dataset.state);
    form.set('week',root.dataset.week);form.set('revision',data.revision);form.set('action',action);form.set('plan',JSON.stringify(plan));
    if(action==='approve') {form.set('driver_id',approval.querySelector('[data-week-driver]').value);form.set('reviewed','yes');}
    saving=true;message.textContent='Saving schedule…';root.setAttribute('aria-busy','true');
    try {
      const response=await fetch(root.dataset.saveUrl,{method:'POST',body:form,headers:{Accept:'application/json'}});
      const result=await response.json().catch(()=>({error:'The form expired or the server could not save. Keep this page open and try again.'}));
      if(!response.ok)throw new Error(result.error||'Unable to save the schedule.');
      dirty=false;sessionStorage.setItem('bf-week-saved',result.message);location.reload();
    } catch(exc) {message.textContent=exc.message;error.textContent=exc.message;}
    finally {saving=false;root.removeAttribute('aria-busy');}
  }
  root.querySelector('[data-week-save]').addEventListener('click',()=>save('save'));
  approval.querySelector('[data-week-approve]').addEventListener('click',()=>save('approve'));
  approval.querySelector('[data-approval-state]').addEventListener('change',event=>{location.href=event.target.value;});
  const templateDialog=document.getElementById('week-template'), loadDialog=document.getElementById('week-load');
  templateDialog.querySelector('[data-template-save]').addEventListener('click',async()=>{
    if(saving)return;
    const error=templateDialog.querySelector('[data-template-error]');error.textContent='';
    const name=templateDialog.querySelector('[data-template-name]').value.trim();
    if(!name){error.textContent='Enter a name for this schedule.';return;}
    const form=new FormData();form.set('csrf',document.getElementById('week-csrf').value);
    form.set('state',root.dataset.state);form.set('week',root.dataset.week);form.set('revision',data.revision);
    form.set('plan',JSON.stringify(plan));form.set('name',name);saving=true;
    try {
      const response=await fetch(root.dataset.templateUrl,{method:'POST',body:form,headers:{Accept:'application/json'}});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'Unable to save the named schedule.');
      const option=el('option',result.name+' · v'+result.version);option.value='template:'+result.id;
      loadDialog.querySelector('[data-template-options]').prepend(option);
      loadDialog.querySelector('[data-load-choice]').value=option.value;
      message.textContent=result.message;templateDialog.close();
    } catch(exc) {error.textContent=exc.message||'The connection failed. Try again.';}
    finally {saving=false;}
  });
  loadDialog.querySelector('[data-schedule-load]').addEventListener('click',async()=>{
    if(saving)return;
    const error=loadDialog.querySelector('[data-load-error]');error.textContent='';
    const selected=loadDialog.querySelector('[data-load-choice]').value;
    if(!selected){error.textContent='Choose a saved schedule or an earlier version of this week.';return;}
    const [kind,id]=selected.split(':'),url=new URL(root.dataset.loadUrl,location.origin);
    url.searchParams.set('state',root.dataset.state);url.searchParams.set('week',root.dataset.week);url.searchParams.set(kind,id);
    saving=true;
    try {
      const response=await fetch(url,{headers:{Accept:'application/json'}});const result=await response.json();
      if(!response.ok)throw new Error(result.error||'Unable to load that schedule.');
      Object.assign(plan,result.plan);mark();draw();message.textContent=result.message;loadDialog.close();
    } catch(exc) {error.textContent=exc.message||'The connection failed. Try again.';}
    finally {saving=false;}
  });
  const clearDialog=document.getElementById('week-clear');
  let clearToken=null, clearRequest=0;
  async function previewClear() {
    const sequence=++clearRequest;
    const error=clearDialog.querySelector('[data-clear-error]'),summary=clearDialog.querySelector('[data-clear-summary]');
    const confirm=clearDialog.querySelector('[data-clear-confirm]');confirm.disabled=true;clearToken=null;error.textContent='';summary.textContent='Checking the selected dates…';
    const url=new URL(root.dataset.clearPreviewUrl,location.origin);
    url.searchParams.set('state',root.dataset.state);url.searchParams.set('period',clearDialog.querySelector('[data-clear-period]').value);
    url.searchParams.set('date',clearDialog.querySelector('[data-clear-date]').value);
    try {
      const response=await fetch(url,{headers:{Accept:'application/json'}}),result=await response.json();
      if(sequence!==clearRequest)return;
      if(!response.ok)throw new Error(result.error||'Unable to preview these dates.');
      summary.textContent=result.state+' · '+result.first+' to '+result.last+' · '+result.items+' calendar items · '+result.jobs+' jobs / allocations · '+result.published+' published weeks affected.';
      confirm.textContent='Clear '+result.state+' '+result.period;
      if(result.blocked){error.textContent=result.blocked+' started or completed jobs protect this period from clearing.';return;}
      clearToken=result.token;confirm.disabled=false;
    } catch(exc) {if(sequence===clearRequest){summary.textContent='';error.textContent=exc.message||'Unable to preview the clear.';}}
  }
  root.querySelector('[data-calendar-open="week-clear"]').addEventListener('click',previewClear);
  clearDialog.querySelector('[data-clear-refresh]').addEventListener('click',previewClear);
  clearDialog.querySelector('[data-clear-period]').addEventListener('change',previewClear);
  clearDialog.querySelector('[data-clear-date]').addEventListener('change',previewClear);
  clearDialog.querySelector('[data-clear-confirm]').addEventListener('click',async()=>{
    if(saving||!clearToken)return;
    const form=new FormData();form.set('csrf',document.getElementById('week-csrf').value);form.set('state',root.dataset.state);
    form.set('period',clearDialog.querySelector('[data-clear-period]').value);form.set('date',clearDialog.querySelector('[data-clear-date]').value);form.set('token',clearToken);
    saving=true;clearDialog.querySelector('[data-clear-confirm]').disabled=true;
    try {
      const response=await fetch(root.dataset.clearUrl,{method:'POST',body:form,headers:{Accept:'application/json'}}),result=await response.json();
      if(!response.ok)throw new Error(result.error||'Unable to clear the schedule.');
      dirty=false;sessionStorage.setItem('bf-week-saved',result.message);location.reload();
    } catch(exc) {clearDialog.querySelector('[data-clear-error]').textContent=exc.message;clearToken=null;}
    finally {saving=false;}
  });
  document.addEventListener('dragend',()=>{dragged=null;grid.querySelectorAll('.bf-drop-target').forEach(e=>e.classList.remove('bf-drop-target'));});
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  draw();
  const clearPeriod=new URL(location.href).searchParams.get('clear');
  if(['week','month'].includes(clearPeriod)){const cleanUrl=new URL(location.href);cleanUrl.searchParams.delete('clear');history.replaceState(null,'',cleanUrl);clearDialog.querySelector('[data-clear-period]').value=clearPeriod;clearDialog.showModal();previewClear();}
  const saved=sessionStorage.getItem('bf-week-saved');if(saved){message.textContent=saved;sessionStorage.removeItem('bf-week-saved');}
})();
