'use strict';
(() => {
  const root = document.querySelector('[data-calendar-editor]');
  if (!root) return;
  const panel = document.getElementById('calendar-expanded-day');
  const moveDialog = document.getElementById('calendar-stop-move');
  const message = root.querySelector('[data-calendar-message]');
  const weeks = new Map(), loading = new Map();
  let moves = [], dragged = null, expanded = null, movingStop = null, saving = false;
  let openSequence = 0, actionPending = false;
  const el = (tag, text, css) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (css) node.className = css;
    return node;
  };
  const button = (text, action, css = 'secondary') => {
    const node = el('button', text, css); node.type = 'button';
    node.addEventListener('click', action); return node;
  };
  const iso = date => date.toISOString().slice(0, 10);
  const shift = (day, count) => { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + count); return iso(d); };
  const monday = day => { const d = new Date(day + 'T12:00:00Z'); return shift(day, -((d.getUTCDay() + 6) % 7)); };
  const identity = (state, day) => state + ':' + monday(day);
  const dateLabel = day => new Intl.DateTimeFormat('en-AU', {weekday:'long',day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));
  const dayAt = (state, day) => weeks.get(identity(state,day))?.plan.days.find(d => d.date === day);
  const outstanding = (week, day) => {
    const scheduled = new Set(week.plan.days.flatMap(d=>d.items).map(i=>i.profile_id).filter(Boolean));
    return (week.plan.backlog||[]).filter(item=>item.date===day&&!scheduled.has(item.profile_id));
  };
  const say = (text, error = false) => {
    message.textContent = text; message.classList.toggle('is-error', error);
    const panelError = panel.querySelector('[data-expanded-error]');
    panelError.hidden = !error; panelError.textContent = error ? text : '';
  };
  const changed = () => {
    document.querySelectorAll('[data-calendar-save]').forEach(b => { b.disabled = saving || actionPending || !moves.length; });
    root.querySelector('[data-calendar-undo]').disabled = saving || actionPending || !moves.length;
  };
  async function ensure(state, day) {
    if (weeks.has(identity(state, day))) return;
    const first = day >= root.dataset.first && day <= root.dataset.last ? root.dataset.first : day;
    const last = first === root.dataset.first ? root.dataset.last : day;
    const cacheKey = state + ':' + first + ':' + last;
    if (!loading.has(cacheKey)) {
      const url = new URL(root.dataset.loadUrl, location.origin);
      url.searchParams.set('state', state); url.searchParams.set('first', first); url.searchParams.set('last', last);
      loading.set(cacheKey, (async () => {
        const response = await fetch(url, {headers:{Accept:'application/json'}});
        const result = await response.json().catch(() => ({error:'Your session expired. Reload the calendar.'}));
        if (!response.ok) throw Error(result.error || 'Unable to load the calendar.');
        for (const week of result.weeks) if (!weeks.has(week.key)) {
          week.original = structuredClone(week.plan); weeks.set(week.key, week);
        }
      })().finally(() => loading.delete(cacheKey)));
    }
    await loading.get(cacheKey);
    if (!weeks.has(identity(state,day))) throw Error('Choose a date from 26 October 2026 onwards.');
  }
  function apply(op) {
    const a = dayAt(op.state,op.source), b = dayAt(op.state,op.target);
    if (!a || !b) throw Error('Load both dates before moving work.');
    const crossWeek = monday(op.source) !== monday(op.target);
    if (op.kind === 'day') {
      [a.items,b.items] = [b.items,a.items]; [a.notes,b.notes] = [b.notes,a.notes];
      if (crossWeek) for (const [label,day] of [['a',a],['b',b]]) day.items.forEach((item,n) => {item.id='cal-'+op.uid+'-'+label+n;});
    } else {
      const index = a.items.findIndex(i => i.id === op.item_id);
      if (index < 0) throw Error('That stop has changed. Reopen the day.');
      const [item] = a.items.splice(index,1);
      const before = b.items.findIndex(i => i.id === op.before);
      if (crossWeek) item.id='cal-'+op.uid+'-item';
      b.items.splice(before < 0 ? b.items.length : before,0,item);
    }
    a.estimate_valid = b.estimate_valid = false;
  }
  function validateDay(day) {
    const visits = new Set(), jobs = new Set(); let baling = 0;
    for (const i of day.items) {
      if (i.kind === 'visit') {
        if (visits.has(i.profile_id)) throw Error('This client already has a visit on that date. Choose another day.');
        visits.add(i.profile_id);
      }
      if (i.kind === 'job') jobs.add(i.site_id);
      if (i.kind === 'baling' && ++baling > 1) throw Error('That day already has a baling block. Swap the whole days instead.');
    }
    if (day.items.some(i => i.kind === 'visit' && jobs.has(i.site_id))) throw Error('That date already has a job for this client. Expand that day to review its existing stop.');
    if (day.items.length > 100) throw Error('A day can contain at most 100 stops.');
  }
  function rebuild() {
    for (const week of weeks.values()) week.plan=structuredClone(week.original);
    moves.forEach(apply);
  }
  async function stage(op) {
    if (saving || actionPending) return false;
    if (op.source === op.target && (op.kind === 'day' || op.item_id === op.before)) return false;
    if (moves.length >= 200) {say('Save your current changes before making more moves.', true); return false;}
    actionPending=true;changed();
    try {
      await ensure(op.state, op.source); await ensure(op.state, op.target);
      op.uid=crypto.randomUUID().replaceAll('-','');
      apply(op); validateDay(dayAt(op.state,op.source)); validateDay(dayAt(op.state,op.target));
      moves.push(op);draw();
      say(moves.length+' unsaved '+(moves.length===1?'move':'moves')+'. Review the dates, then Save changes.');
      return true;
    } catch (error) { rebuild();draw();say(error.message || 'Unable to move this work.',true);return false; }
    finally {actionPending=false;changed();}
  }
  function grip(state,day,item) {
    const node=button(item?'⠿ Drag stop':'⠿ Move '+state+' day',()=>item?openMove(state,day,item.id):openDay(state,day),item?'bf-stop-grip':'bf-day-grip');
    node.draggable=true;node.dataset.dragState=state;node.dataset.dragDate=day;
    if(item)node.dataset.dragItem=item.id;
    node.setAttribute('aria-label',item?'Move '+item.title:'Move whole '+state+' day '+day);
    return node;
  }
  function draw() {
    const dirtyDates=new Set(moves.flatMap(op=>[op.state+':'+op.source,op.state+':'+op.target]));
    for(const week of weeks.values()) for(const day of week.plan.days) {
      const cell=root.querySelector('td[data-date="'+day.date+'"]');if(!cell)continue;
      const list=cell.querySelector('.bf-date-events');
      list.querySelectorAll('[data-state="'+week.state+'"], [data-drag-state="'+week.state+'"]').forEach(n=>n.remove());
      const pending=outstanding(week,day.date);
      if(!day.items.length&&!pending.length)continue;
      const card=button('',()=>openDay(week.state,day.date),'bf-cal-event');
      card.dataset.state=week.state;card.setAttribute('aria-controls',panel.id);
      card.setAttribute('aria-label','Expand '+week.state+' '+dateLabel(day.date));
      card.classList.toggle('is-baling',day.items.every(i=>i.kind==='baling'));
      card.classList.toggle('bf-calendar-dirty',dirtyDates.has(week.state+':'+day.date));
      const top=el('span',undefined,'bf-cal-event-top');top.append(el('strong',week.state),el('span',dirtyDates.has(week.state+':'+day.date)?'Unsaved':week.status==='published'?'Published':'Draft','bf-cal-event-hours'));
      card.append(top,el('span',day.items.length+' scheduled '+(day.items.length===1?'item':'items'),'bf-cal-event-title'));
      card.append(el('span',day.items.slice(0,3).map(i=>i.title).join(' · ')+(day.items.length>3?' +'+(day.items.length-3)+' more':''),'bf-calendar-items-preview'));
      if(pending.length){card.classList.add('needs-attention');card.append(el('span',pending.length+' fixed visits need arrangement','bf-cal-event-warning'));}
      if(day.items.length)list.append(grip(week.state,day.date));list.append(card);
    }
    if(expanded)drawPanel();changed();
    root.dispatchEvent(new Event('calendar-progress-redraw'));
  }
  async function openDay(state,day) {
    if(saving || actionPending)return;
    const sequence=++openSequence;
    try {
      say('Loading '+state+' day…');await ensure(state,day);
      if(sequence!==openSequence)return;
      expanded={state,day};panel.hidden=false;root.classList.add('has-expanded-day');root.classList.toggle('has-expanded-wide',panel.classList.contains('is-wide'));
      draw();say(moves.length?'Unsaved moves — Save changes when ready.':'Drag stops onto a date, or use Move to choose another date.');
      panel.querySelector('[data-expanded-close]').focus({preventScroll:true});
    } catch(error){say(error.message||'Unable to open this day.',true);}
  }
  function drawPanel() {
    const {state,day:date}=expanded, day=dayAt(state,date), week=weeks.get(identity(state,date));
    document.getElementById('calendar-expanded-title').textContent=dateLabel(date);
    panel.querySelector('[data-expanded-state]').textContent=state+' · '+day.items.length+' scheduled items';
    panel.querySelector('[data-expanded-region]').value=state;
    panel.querySelector('[data-expanded-status]').textContent=(week.status==='published'?'Published to '+week.driver:'Draft · approval required')+' · '+(day.estimate_valid&&day.estimate_minutes?(day.estimate_minutes/60).toFixed(1)+'h original estimate':'Review route and working hours after changes');
    panel.querySelector('[data-whole-day-target]').value=date;
    panel.querySelector('[data-expanded-notes]').textContent=day.notes||'';
    const list=panel.querySelector('[data-expanded-stops]');list.replaceChildren();
    day.items.forEach((item,n)=>{
      const card=el('article',undefined,'bf-expanded-stop');card.dataset.stopId=item.id;
      card.append(grip(state,date,item),el('small','Stop '+(n+1)+(item.time?' · '+item.time:'')),el('strong',item.title),el('p',item.address),el('p',item.detail));
      if(item.notes)card.append(el('p',item.notes));
      const actions=el('div',undefined,'bf-expanded-stop-actions');
      const up=button('↑',()=>stage({kind:'item',state,source:date,target:date,item_id:item.id,before:day.items[n-1]?.id}));
      up.disabled=n===0;up.setAttribute('aria-label','Move '+item.title+' earlier');
      const down=button('↓',()=>stage({kind:'item',state,source:date,target:date,item_id:item.id,before:day.items[n+2]?.id||null}));
      down.disabled=n===day.items.length-1;down.setAttribute('aria-label','Move '+item.title+' later');
      actions.append(up,down,button('Move…',()=>openMove(state,date,item.id)),button('Edit',()=>requestAction('edit',item.id)));card.append(actions);list.append(card);
    });
    if(!day.items.length)list.append(el('p','No work scheduled. Drop a stop here or choose Add work.','bf-muted'));
    const loads=panel.querySelector('[data-expanded-loads]');loads.replaceChildren();
    if(day.estimate_valid&&day.loads?.length){
      const details=el('details',undefined,'bf-week-loads');details.append(el('summary','Depot loads · '+day.loads.length));
      day.loads.forEach((load,n)=>{details.append(el('p','Load '+(n+1)+' · '+load.start+'–'+load.end));const stops=el('ul');load.stops.forEach(s=>stops.append(el('li',s.title+' · '+s.large+' × 1100L / '+s.small+' × 240L')));details.append(stops,el('p','Return to depot after this load.'));});loads.append(details);
    }
    for(const pending of outstanding(week,date))loads.append(el('p',pending.title+' · Needs arrangement: '+pending.reason,'bf-note'));
  }
  function openMove(state,source,item_id) {
    if(saving||actionPending)return;
    const item=dayAt(state,source)?.items.find(i=>i.id===item_id);if(!item)return;
    movingStop={state,source,item_id};moveDialog.querySelector('[data-move-title]').textContent=item.title;
    moveDialog.querySelector('[data-stop-target]').value=source;
    moveDialog.querySelector('[data-move-error]').textContent='';moveDialog.showModal();
  }
  function requestAction(action,item) {
    document.dispatchEvent(new CustomEvent('bf-calendar-action',{detail:{action,item,state:expanded.state,date:expanded.day,fromDay:true}}));
  }
  panel.querySelectorAll('[data-day-action]').forEach(b=>b.addEventListener('click',()=>requestAction(b.dataset.dayAction)));
  document.addEventListener('bf-calendar-before-action',event=>{
    if(saving||actionPending||moves.length){event.preventDefault();say('Save or undo your calendar moves before editing work or using Calendar options.',true);}
  });
  document.addEventListener('dragstart',event=>{
    const handle=event.target.closest('[data-drag-state]');
    if(!handle||saving||actionPending){if(handle)event.preventDefault();return;}
    dragged={state:handle.dataset.dragState,source:handle.dataset.dragDate,kind:handle.dataset.dragItem?'item':'day'};
    if(handle.dataset.dragItem)dragged.item_id=handle.dataset.dragItem;
    event.dataTransfer.setData('text/plain',JSON.stringify(dragged));event.dataTransfer.effectAllowed='move';
  });
  const clearDrop=()=>document.querySelectorAll('.bf-calendar-drop,.bf-stop-drop').forEach(n=>n.classList.remove('bf-calendar-drop','bf-stop-drop'));
  document.addEventListener('dragend',()=>{dragged=null;clearDrop();});
  root.addEventListener('dragover',event=>{
    const cell=event.target.closest('td[data-date]');
    if(!dragged||!cell||cell.dataset.date<'2026-10-26')return;
    event.preventDefault();event.dataTransfer.dropEffect='move';clearDrop();cell.classList.add('bf-calendar-drop');
  });
  root.addEventListener('dragleave',event=>{if(!root.contains(event.relatedTarget))clearDrop();});
  root.addEventListener('drop',event=>{
    const cell=event.target.closest('td[data-date]');if(!dragged||!cell)return;
    event.preventDefault();const op={...dragged,target:cell.dataset.date};dragged=null;clearDrop();stage(op);
  });
  const stopList=panel.querySelector('[data-expanded-stops]');
  stopList.addEventListener('dragover',event=>{
    if(!dragged||dragged.kind!=='item'||dragged.state!==expanded.state)return;
    event.preventDefault();clearDrop();event.target.closest('[data-stop-id]')?.classList.add('bf-stop-drop');
  });
  stopList.addEventListener('drop',event=>{
    if(!dragged||dragged.kind!=='item'||dragged.state!==expanded.state)return;
    event.preventDefault();const op={...dragged,target:expanded.day,before:event.target.closest('[data-stop-id]')?.dataset.stopId||null};
    dragged=null;clearDrop();stage(op);
  });
  root.addEventListener('click',event=>{
    const expand=event.target.closest('[data-expand-date]');
    if(expand)openDay(expand.dataset.expandState||root.dataset.state||expanded?.state||'QLD',expand.dataset.expandDate);
    const handle=event.target.closest('[data-drag-date]');
    // Server-rendered day handles do not yet have a direct click listener.
    if(handle&&!weeks.has(identity(handle.dataset.dragState,handle.dataset.dragDate)))openDay(handle.dataset.dragState,handle.dataset.dragDate);
  });
  panel.querySelector('[data-expanded-close]').addEventListener('click',()=>{
    openSequence++;expanded=null;panel.hidden=true;root.classList.remove('has-expanded-day','has-expanded-wide');
    root.querySelector('[data-calendar-save]').focus({preventScroll:true});
  });
  panel.querySelector('[data-expanded-wide]').addEventListener('click',event=>{
    const wide=panel.classList.toggle('is-wide');event.currentTarget.setAttribute('aria-pressed',String(wide));event.currentTarget.textContent=wide?'Compact':'Expand';root.classList.toggle('has-expanded-wide',wide);
  });
  panel.querySelector('[data-expanded-previous]').addEventListener('click',()=>openDay(expanded.state,shift(expanded.day,-1)));
  panel.querySelector('[data-expanded-next]').addEventListener('click',()=>openDay(expanded.state,shift(expanded.day,1)));
  panel.querySelector('[data-expanded-region]').addEventListener('change',event=>openDay(event.target.value,expanded.day));
  panel.querySelector('[data-whole-day-move]').addEventListener('submit',async event=>{
    event.preventDefault();const target=panel.querySelector('[data-whole-day-target]').value;
    const op={kind:'day',state:expanded.state,source:expanded.day,target};
    if(await stage(op)){expanded.day=target;drawPanel();}
  });
  moveDialog.querySelector('[data-stop-move-form]').addEventListener('submit',async event=>{
    event.preventDefault();const target=moveDialog.querySelector('[data-stop-target]').value;
    if(target===movingStop.source){moveDialog.querySelector('[data-move-error]').textContent='Choose another date, or use the arrows to reorder this day.';return;}
    if(await stage({...movingStop,kind:'item',target}))moveDialog.close();
    else moveDialog.querySelector('[data-move-error]').textContent=message.textContent;
  });
  root.querySelector('[data-calendar-undo]').addEventListener('click',()=>{
    if(saving||actionPending)return;moves.pop();rebuild();draw();say(moves.length?'Last move undone. Remaining changes are unsaved.':'All moves undone. The saved schedule is unchanged.');
  });
  async function save() {
    if(saving||actionPending||!moves.length)return;
    const tokens={};for(const op of moves)for(const day of [op.source,op.target])tokens[identity(op.state,day)]=weeks.get(identity(op.state,day)).token;
    const form=new FormData();form.set('csrf',root.querySelector('[data-calendar-csrf]').value);form.set('moves',JSON.stringify(moves));form.set('tokens',JSON.stringify(tokens));
    saving=true;changed();root.setAttribute('aria-busy','true');panel.classList.add('is-saving');say('Saving all calendar moves…');
    try{
      const response=await fetch(root.dataset.saveUrl,{method:'POST',body:form,headers:{Accept:'application/json'}});
      const result=await response.json().catch(()=>({error:'Your session expired or the server could not save. Keep this page open and try again.'}));
      if(!response.ok)throw Error(result.error||'The calendar could not be saved.');
      moves=[];sessionStorage.setItem('bf-calendar-saved',result.message);
      if(expanded)sessionStorage.setItem('bf-calendar-expanded',JSON.stringify(expanded));
      location.reload();
    }catch(error){say(error.message||'Connection failed. Your unsaved moves are still on this screen.',true);}
    finally{saving=false;changed();root.removeAttribute('aria-busy');panel.classList.remove('is-saving');}
  }
  document.querySelectorAll('[data-calendar-save]').forEach(b=>b.addEventListener('click',save));
  window.addEventListener('beforeunload',event=>{if(moves.length){event.preventDefault();event.returnValue='';}});
  const saved=sessionStorage.getItem('bf-calendar-saved');if(saved){say(saved);sessionStorage.removeItem('bf-calendar-saved');}
  const restore=sessionStorage.getItem('bf-calendar-expanded');sessionStorage.removeItem('bf-calendar-expanded');
  if(restore){try{const value=JSON.parse(restore);openDay(value.state,value.day).then(()=>{if(saved)say(saved);});}catch(_){/* Ignore an obsolete view preference. */}}
  else if(root.dataset.state&&root.dataset.last>='2026-10-26')ensure(root.dataset.state,root.dataset.last).then(draw).catch(error=>say(error.message,true));
})();
