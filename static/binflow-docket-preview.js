'use strict';
(() => {
  let active, timer;
  function close() {
    clearTimeout(timer);
    if(active) active.hidePopover();
    active=null;
  }
  for(const link of document.querySelectorAll('[data-docket-trigger]')) {
    const card=document.getElementById(link.getAttribute('aria-describedby'));
    if(!card.showPopover) {link.title=card.textContent.trim(); continue;}
    function show() {
      close(); active=card; card.showPopover();
      const r=link.getBoundingClientRect(), pad=8;
      card.style.left=Math.max(pad,Math.min(r.left,innerWidth-card.offsetWidth-pad))+'px';
      card.style.top=(r.bottom+card.offsetHeight+pad<innerHeight?r.bottom+4:Math.max(pad,r.top-card.offsetHeight-4))+'px';
    }
    const later=()=>{timer=setTimeout(close,160);};
    link.addEventListener('pointerenter',e=>{if(e.pointerType!=='touch') show();});
    link.addEventListener('pointerleave',later);
    link.addEventListener('focus',show); link.addEventListener('blur',later);
    card.addEventListener('pointerenter',()=>clearTimeout(timer)); card.addEventListener('pointerleave',later);
  }
  document.addEventListener('keydown',e=>{if(e.key==='Escape') close();});
  window.addEventListener('resize',close);
  document.addEventListener('scroll',()=>{
    if(active) {
      const anchor=document.querySelector('[aria-describedby="'+active.id+'"]');
      if(!anchor?.matches(':hover,:focus') && !active.matches(':hover')) close();
    }
  },true);
})();
