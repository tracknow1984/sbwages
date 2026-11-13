'use strict';
(() => {
  const popup=document.getElementById('prestart-preview');
  if(!popup) return;
  const content=popup.querySelector('[data-preview-content]');
  const closeButton=popup.querySelector('[data-preview-close]');
  let active=null, request=null, hoverTimer, closeTimer, pinned=false, returning=false;
  function close(restore=false) {
    clearTimeout(hoverTimer); clearTimeout(closeTimer);
    request?.abort();
    const trigger=active;
    active=null; pinned=false;
    trigger?.setAttribute('aria-expanded','false');
    if(popup.matches(':popover-open')) popup.hidePopover();
    if(restore && trigger) {
      returning=true; trigger.focus({preventScroll:true}); returning=false;
    }
  }
  function leave() {
    clearTimeout(hoverTimer); clearTimeout(closeTimer);
    if(!pinned) closeTimer=setTimeout(()=>close(),450);
  }
  async function show(trigger, pin=false) {
    clearTimeout(closeTimer); clearTimeout(hoverTimer);
    if(!popup.showPopover) {if(pin) location.assign(trigger.dataset.previewUrl.split('?')[0]);return;}
    if(active===trigger) {if(pin) {pinned=true;closeButton.focus();} return;}
    close();
    active=trigger; pinned=pin; trigger.setAttribute('aria-expanded','true');
    const url=new URL(trigger.dataset.previewUrl, location.href);
    url.searchParams.delete('preview'); popup.querySelector('[data-full-prestart]').href=url;
    content.textContent='Loading signed prestart…'; popup.hidden=false; popup.showPopover();
    // Place the preview against the selected row so the pointer can enter it directly.
    if(matchMedia('(min-width:641px)').matches) {
      const rect=trigger.closest('[data-inspection-row]').getBoundingClientRect();
      popup.style.top=Math.max(8,Math.min(rect.bottom-3,innerHeight-Math.min(innerHeight*.78,672)-12))+'px';
      popup.style.bottom='auto';
    } else {popup.style.removeProperty('top');popup.style.removeProperty('bottom');}
    if(pin) closeButton.focus();
    const controller=new AbortController(); request=controller;
    try {
      const response=await fetch(trigger.dataset.previewUrl,{signal:controller.signal,cache:'no-store'});
      if(!response.ok || response.redirected) throw Error();
      const doc=new DOMParser().parseFromString(await response.text(),'text/html');
      const record=doc.querySelector('[data-signed-prestart]');
      if(!record) throw Error();
      if(active===trigger && !controller.signal.aborted) {content.replaceChildren(record);content.scrollTop=0;}
    } catch {
      if(!controller.signal.aborted && active===trigger) content.textContent='Preview unavailable. Open the signed record below.';
    }
  }
  document.querySelectorAll('[data-inspection-row]').forEach(row=>{
    const trigger=row.querySelector('[data-preview-url]');
    row.addEventListener('pointerenter',e=>{
      if(e.pointerType==='touch'||pinned) return;
      clearTimeout(hoverTimer);clearTimeout(closeTimer);hoverTimer=setTimeout(()=>show(trigger),220);
    });
    row.addEventListener('pointerleave',leave);
    trigger.addEventListener('focus',()=>{if(!returning && trigger.matches(':focus-visible')) show(trigger);});
    trigger.addEventListener('click',()=>{if(active===trigger && pinned) close(true);else show(trigger,true);});
  });
  popup.addEventListener('pointerenter',()=>clearTimeout(closeTimer));
  popup.addEventListener('pointerleave',leave);
  closeButton.addEventListener('click',()=>close(true));
  document.addEventListener('keydown',e=>{if(e.key==='Escape' && active) close(pinned || popup.contains(document.activeElement));});
  document.addEventListener('focusin',e=>{if(active && e.target!==active && !popup.contains(e.target)) close();});
  document.addEventListener('pointerdown',e=>{if(active && !popup.contains(e.target) && !active.closest('[data-inspection-row]').contains(e.target)) close();});
})();
