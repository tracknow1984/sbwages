'use strict';
(() => {
  const root=document.querySelector('[data-calendar-editor]');
  if(!root)return;
  const dialog=document.getElementById('calendar-action-dialog'),body=dialog.querySelector('[data-calendar-action-body]');
  const form=dialog.querySelector('form'),submit=dialog.querySelector('[data-calendar-action-submit]'),error=dialog.querySelector('[role=alert]');
  let context,options,action,commit,sequence=0,busy=false;
  const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const field=(label,type,value='')=>{
    const wrap=node('label',label),input=node(type==='select'?'select':type==='textarea'?'textarea':'input');
    if(input.tagName==='INPUT')input.type=type;
    input.setAttribute('aria-label',label);input.value=value;wrap.append(input);body.append(wrap);return input;
  };
  const choices=(select,rows)=>{select.replaceChildren(...rows.map(([value,label])=>{const o=node('option',label);o.value=value;return o;}));};
  const note=text=>body.append(node('p',text));
  async function api(path,data) {
    const settings={headers:{Accept:'application/json'}};
    if(data){const payload=new FormData();payload.set('csrf',root.querySelector('[data-calendar-csrf]').value);for(const [k,v] of Object.entries(data))payload.set(k,v);settings.method='POST';settings.body=payload;}
    const response=await fetch('/binflow/schedule/'+path,settings);
    const result=await response.json().catch(()=>({error:'Your session expired. Reload the calendar.'}));
    if(!response.ok)throw Error(result.error||'Unable to update the calendar.');return result;
  }
  const base=()=>({state:options.state,week:options.start,revision:options.revision,plan:JSON.stringify(options.plan)});
  const save=(extra={})=>api('save',{...base(),action:'save',...extra});
  function finish(result){sessionStorage.setItem('bf-calendar-saved',result.message);if(context.fromDay)sessionStorage.setItem('bf-calendar-expanded',JSON.stringify({state:context.state,day:context.date}));location.reload();}
  async function loadOptions(){
    const current=++sequence;submit.disabled=true;error.textContent='';commit=null;body.replaceChildren(node('p','Loading…'));
    try {
      const result=await api('options?'+new URLSearchParams({state:context.state,date:context.date}));
      if(current!==sequence||!dialog.open)return;
      options=result;body.replaceChildren();
      if(context.action==='options')await drawOptions();else drawDay();
    } catch(e){if(current===sequence)error.textContent=e.message;}
  }
  async function drawOptions(){
    const current=sequence;
    const state=field('State','select');choices(state,['QLD','NSW','VIC','SA','WA','TAS','ACT','NT'].map(s=>[s,s]));state.value=context.state;
    const date=field(context.approvalOnly?'Week containing':'Date within the week / month','date',context.date);date.min='2026-10-26';date.max='2100-12-31';date.required=true;
    state.onchange=()=>{context.state=state.value;loadOptions();};date.onchange=()=>{if(date.validity.valid){context.date=date.value;loadOptions();}};
    if(!context.approvalOnly){
      const select=field('Action','select');choices(select,[['approve','Approve schedule'],['template','Save reusable schedule'],['load','Load saved schedule'],['clear','Clear dates']]);select.value=action;
      select.onchange=()=>{action=select.value;loadOptions();};
    }
    note(options.state+' · Week of '+options.start+' · '+(options.status==='published'?'Published':'Draft'));
    if(action==='approve'){
      const driver=field('Driver','select');choices(driver,[['','Select driver'],...options.drivers.map(d=>[d.id,d.first_name+' '+d.last_name])]);driver.value=options.driver_id||'';driver.required=true;
      if(!options.drivers.length)note('No active driver or contractor is allocated to '+options.state+'. Set their working state in their staff profile.');
      note('Includes every day in this week. Pending bin allocations are approved subject to stock checks. Visits need bin allocations before loading.');
      const checked=field('I have checked the dates, working hours, depot loads and client access.','checkbox');checked.required=true;
      submit.textContent=context.approvalOnly?'Approve week':'Approve schedule';submit.disabled=!options.drivers.length;
      commit=()=>save({action:'approve',driver_id:driver.value,reviewed:checked.checked?'yes':''});
    }else if(action==='template'){
      const name=field('Schedule name','text');name.maxLength=80;name.required=true;
      note('Saves this week for reuse in '+options.state+'. Jobs become client visits; future weeks need their own bin allocations.');
      submit.textContent='Save reusable schedule';submit.disabled=false;commit=()=>api('templates',{...base(),name:name.value});
    }else if(action==='load'){
      const choice=field('Saved schedule','select');choices(choice,[['','Select a schedule'],...options.templates.map(t=>['template:'+t.id,t.name+' · version '+t.version]),...options.history.map(h=>['revision:'+h.revision,'This week · version '+h.revision+' · '+h.action])]);choice.required=true;
      note('Preview the replacement before applying it. Published schedules update when applied.');
      const preview=node('div');body.append(preview);let loaded;
      choice.onchange=async()=>{
        const selection=choice.value;loaded=null;submit.disabled=true;preview.replaceChildren();error.textContent='';if(!selection)return;
        try{const [kind,id]=selection.split(':');const result=await api('load?'+new URLSearchParams({state:options.state,week:options.start,[kind]:id}));
          if(current!==sequence||selection!==choice.value||!dialog.open)return;
          loaded=result.plan;for(const day of loaded.days)preview.append(node('p',day.date+': '+(day.items.map(i=>i.title).join(' · ')||'No work scheduled')));submit.disabled=false;
        }catch(e){if(current===sequence)error.textContent=e.message;}
      };
      submit.textContent='Apply schedule';submit.disabled=true;commit=()=>{if(!loaded)throw Error('Choose and preview a saved schedule.');return save({plan:JSON.stringify(loaded)});};
    }else{
      const period=field('Period','select');choices(period,[['week','One week'],['month','One calendar month']]);
      const summary=node('p');body.append(summary);let token;
      async function preview(){token=null;submit.disabled=true;error.textContent='';summary.textContent='Checking dates…';const periodValue=period.value;
        try{const result=await api('clear-preview?'+new URLSearchParams({state:context.state,date:context.date,period:periodValue}));
          if(current!==sequence||period.value!==periodValue||!dialog.open)return;
          token=result.token;summary.textContent=result.first+' to '+result.last+' · '+result.items+' planned items · '+result.published+' published weeks.';
          if(result.blocked)summary.textContent+=' Started or completed jobs prevent clearing this period.';submit.disabled=!!result.blocked;
        }catch(e){if(current===sequence)error.textContent=e.message;}
      }
      note('Clears the selected dates and updates driver schedules. Job records, bin reservations and previous versions are kept.');
      period.onchange=preview;submit.textContent='Clear selected dates';commit=()=>{if(!token)throw Error('Wait for the date check.');return api('clear',{state:context.state,date:context.date,period:period.value,token});};await preview();
    }
  }
  function drawDay(){
    const day=options.plan.days.find(d=>d.date===context.date);if(!day)throw Error('Choose a date in this week.');
    note(context.state+' · '+context.date);
    if(context.action==='notes'){
      const notes=field('Day notes','textarea',day.notes||'');notes.maxLength=1000;submit.textContent='Save notes';
      commit=()=>{day.notes=notes.value;return save();};
    }else if(context.action==='edit'){
      const item=day.items.find(i=>i.id===context.item);if(!item)throw Error('This stop changed. Reload the calendar.');
      note(item.title);const time=field('Arrival time','time',item.time||'');time.min='06:30';time.max='15:30';
      const notes=field('Instructions','textarea',item.notes||'');notes.maxLength=1000;
      const remove=field('Remove this stop from the day','checkbox');submit.textContent='Save stop';
      commit=()=>{if(remove.checked)day.items=day.items.filter(i=>i.id!==item.id);else{item.time=time.value;item.notes=notes.value;}return save();};
    }else{
      const kind=field('Work type','select');choices(kind,[['visit','Client visit'],['job','Existing job / bin allocation'],['baling','Baling & depot work']]);
      const choice=field('Work','select');let candidates=[];
      function update(){candidates=kind.value==='visit'?options.sites:options.jobs;choice.parentElement.hidden=kind.value==='baling';choices(choice,[['','Choose work'],...candidates.map((i,n)=>[n,i.title])]);choice.required=kind.value!=='baling';}
      kind.onchange=update;update();submit.textContent='Add work';
      commit=()=>{const item=kind.value==='baling'?{kind:'baling',time:'06:30',notes:''}:structuredClone(candidates[Number(choice.value)]);
        if(!item||kind.value!=='baling'&&choice.value==='')throw Error('Choose work to add.');
        item.id='calendar-'+crypto.randomUUID().replaceAll('-','');day.items.push(item);return save();};
    }
    submit.disabled=false;
  }
  async function open(detail){
    if(busy||dialog.open)return;
    if(!document.dispatchEvent(new CustomEvent('bf-calendar-before-action',{cancelable:true})))return;
    context={...detail,state:detail.state||root.dataset.state||'QLD',date:detail.date||root.dataset.chosen};action='approve';
    dialog.querySelector('h2').textContent=context.approvalOnly?'Approve week':{options:'Calendar options',add:'Add work',edit:'Edit stop',notes:'Day notes'}[context.action];
    dialog.showModal();await loadOptions();
  }
  root.querySelectorAll('[data-calendar-action]').forEach(button=>button.addEventListener('click',()=>open({action:'options',approvalOnly:button.dataset.calendarAction==='approve'})));
  document.addEventListener('bf-calendar-action',event=>open(event.detail));
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||submit.disabled||!commit)return;
    busy=true;submit.disabled=true;error.textContent='';
    const controls=[...dialog.querySelectorAll('input,select,textarea,button')].map(n=>[n,n.disabled]);controls.forEach(([n])=>n.disabled=true);
    // Work on a copy so retrying a rejected addition does not duplicate it.
    const original=structuredClone(options.plan);
    try{finish(await commit());}catch(e){options.plan=original;error.textContent=e.message;submit.disabled=false;
      // Day editors hold references into their plan. Reopen from saved data for a safe retry.
      if(context.action!=='options'){body.replaceChildren();drawDay();}
    }finally{busy=false;controls.forEach(([n,disabled])=>n.disabled=disabled);submit.disabled=false;}
  });
  dialog.addEventListener('click',event=>{if(busy)event.stopImmediatePropagation();},true);
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>{sequence++;});
})();
