'use strict';
(() => {
  const dialog=document.querySelector('[data-allocation-dialog]');
  if(!dialog||!dialog.showModal)return;
  const body=dialog.querySelector('[data-allocation-body]'),error=dialog.querySelector('[data-allocation-error]');
  const close=dialog.querySelector('[data-allocation-close]');
  let url,controller,busy=false,opener,sequence=0;
  async function load(target,settings={}){
    const ticket=++sequence;controller?.abort();controller=new AbortController();
    error.hidden=true;
    try{
      const response=await fetch(target,{...settings,signal:controller.signal});
      if(ticket!==sequence||!dialog.open)return;
      const final=new URL(response.url);
      if(response.ok&&settings.method==='POST'&&/^\/binflow\/pickups\/\d+$/.test(final.pathname)){
        sessionStorage.setItem('bf-calendar-saved','Allocation saved for approval.');location.reload();return;
      }
      const doc=new DOMParser().parseFromString(await response.text(),'text/html');
      if(ticket!==sequence||!dialog.open)return;
      const content=doc.querySelector('[data-allocation-content]');
      if(!response.ok||!content)throw Error(response.status===403?'You do not have permission to allocate this work.':'Unable to load the allocation. Your session may have expired; reload the calendar.');
      url=final;body.replaceChildren();
      doc.querySelectorAll('.notice[role="alert"]').forEach(n=>body.append(document.importNode(n,true)));
      body.append(document.importNode(content,true));
      document.dispatchEvent(new CustomEvent('bf-allocation-loaded',{detail:body}));
      const state=body.querySelector('[name="state"]')?.value||'Choose state',day=body.querySelector('[name="date"]')?.value||'';
      dialog.querySelector('h2').textContent='Add allocation · '+state+' · '+day;
      body.querySelector('select, input')?.focus();
    }catch(e){if(e.name!=='AbortError'){error.textContent=e.message;error.hidden=false;}}
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest('[data-allocation-popup]');
    if(!link||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    if(!document.dispatchEvent(new CustomEvent('bf-calendar-before-action',{cancelable:true}))){event.preventDefault();return;}
    event.preventDefault();opener=link;body.textContent='Loading allocation…';dialog.showModal();load(link.href);
  });
  body.addEventListener('change',event=>{
    if(busy)return;
    if(event.target.matches('[data-allocation-state], [data-allocation-depot]')){
      const form=event.target.form,params=new URLSearchParams(new FormData(form));
      if(event.target.matches('[data-allocation-state]')){params.delete('site');params.delete('warehouse');}
      load('/binflow/pickups/new?'+params);
    }
  });
  body.addEventListener('submit',async event=>{
    const form=event.target;if(!(form instanceof HTMLFormElement))return;
    event.preventDefault();if(busy)return;
    const payload=new FormData(form);if(event.submitter?.name)payload.set(event.submitter.name,event.submitter.value);
    if(form.method.toLowerCase()==='get'){await load('/binflow/pickups/new?'+new URLSearchParams(payload));return;}
    busy=true;close.disabled=true;
    const controls=[...body.querySelectorAll('button,input,select,textarea')].map(n=>[n,n.disabled]);controls.forEach(([n])=>n.disabled=true);
    try{await load(url,{method:'POST',body:payload});}
    finally{busy=false;close.disabled=false;controls.forEach(([n,disabled])=>n.disabled=disabled);}
  });
  close.addEventListener('click',()=>{if(!busy)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>{sequence++;controller?.abort();body.replaceChildren();opener?.focus();});
})();
