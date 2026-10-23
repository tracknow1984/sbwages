'use strict';
(() => {
  // Decorative CSS icons leave labels, click targets and form behaviour intact.
  const paths={
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',back:'<path d="M20 12H4m6-6-6 6 6 6"/>',
    overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    bin:'<path d="M6 7h12l-1 13H7L6 7ZM4 7h16M9 7V4h6v3M10 11v5m4-5v5"/>',
    scan:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 12h18M8 7v2m8-2v2M8 15v2m8-2v2"/>',
    calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18m-14 4h3m4 0h3m-10 4h3"/>',
    requests:'<path d="M4 4h16v12h-6l-5 4v-4H4V4Z"/><path d="M8 8h8m-8 4h5"/>',
    pod:'<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h5"/>',
    allocations:'<rect x="3" y="4" width="6" height="6" rx="1"/><rect x="15" y="14" width="6" height="6" rx="1"/><path d="M14 5h6v6M4 14v6h6M10 8l10-3M4 20l10-3"/>',
    reports:'<path d="M4 3v18h17M8 16v-5m5 5V7m5 9V4"/>',
    clients:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5v2"/>',
    settings:'<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3" fill="white"/><circle cx="16" cy="17" r="3" fill="white"/>',
    truck:'<path d="M3 6h11v11H3zM14 10h4l3 4v3h-7"/><circle cx="7" cy="18" r="2"/><circle cx="18" cy="18" r="2"/>',
    warehouse:'<path d="m3 9 9-6 9 6v12H3V9Zm4 12V11h10v10M7 15h10m-10 3h10"/>',
    pin:'<path d="M20 10c0 6-8 11-8 11S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
    edit:'<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"/>',
    add:'<path d="M12 4v16M4 12h16"/>',save:'<path d="M4 3h13l4 4v14H3V3h1Zm3 0v6h9V3M7 21v-8h10v8"/>',
    download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    upload:'<path d="M12 15V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    print:'<path d="M7 8V3h10v5M6 17H3V9h18v8h-3M7 14h10v7H7zM17 11h1"/>',
    mail:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
    archive:'<path d="M4 8v13h16V8M3 3h18v5H3zM9 12h6"/>',
    delete:'<path d="M4 7h16M9 7V3h6v4M6 7l1 14h10l1-14M10 11v6m4-6v6"/>',
    check:'<path d="m4 12 5 5L20 5"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',
    search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
    refresh:'<path d="M20 7a9 9 0 1 0 1 8M20 3v5h-5"/>',
    lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4m-4 5v2"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    map:'<path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2V5Zm6-2v16m6-14v16"/>',
    filter:'<path d="M3 4h18l-7 8v7l-4 2v-9L3 4Z"/>',
    undo:'<path d="M8 5 3 10l5 5M3 10h11a6 6 0 0 1 0 12"/>',
    logout:'<path d="M9 3H3v18h6m-2-9h14m-5-5 5 5-5 5"/>',
    help:'<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 1 1 5 3c-2 1-2 2-2 3m0 3h.01"/>'
  };
  const urls=Object.fromEntries(Object.entries(paths).map(([name,path])=>[name,'url("data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+path+'</svg>')+'")']));
  const rules=[[/^(←|back|previous)/i,'back'],[/sign out|exit sample/i,'logout'],[/sign.in|login|password/i,'lock'],[/undo|restore/i,'undo'],[/delete|remove|disapprove|dismiss/i,'delete'],[/archive/i,'archive'],[/print/i,'print'],[/email|send/i,'mail'],[/download|export|pdf/i,'download'],[/upload|import/i,'upload'],[/approve|confirm|accept|finish|complete|process/i,'check'],[/save/i,'save'],[/refresh|retry|check again/i,'refresh'],[/scan|qr|labels/i,'scan'],[/add |new |^\+/i,'add'],[/edit|correct/i,'edit'],[/filter|show |apply/i,'filter'],[/find|search/i,'search'],[/calendar|schedule|week|month|today/i,'calendar'],[/reports|timing/i,'reports'],[/pod|docket|documents/i,'pod'],[/client|contact|staff|details/i,'clients'],[/warehouse|depot|inbound/i,'warehouse'],[/truck|vehicle|driver day|prestart/i,'truck'],[/request/i,'requests'],[/allocation/i,'allocations'],[/gps|map|geofence|navigate/i,'map'],[/setup|setting|options/i,'settings'],[/stock|bin|inventory/i,'bin'],[/timesheet|history|break/i,'clock'],[/overview|office/i,'overview'],[/close|cancel/i,'close']];
  function decorate(el){
    if(el.closest('.leaflet-container,.bf-label,.bf-pod,.bf-state-tabs,[data-no-icon]') || el.querySelector('svg,img,canvas,h1,h2,h3,p,div') || el.matches('.brand,.bf-qr-action,[data-drag-date],[data-drag-state],[data-calendar-event]'))return;
    const label=el.textContent.trim();
    if(!label || /^[\d\s.\/↑↓↔×✕‹›<>−–+-]+$/.test(label))return;
    const name=el.dataset.bfIcon || rules.find(([pattern])=>pattern.test(label))?.[1] || (el.tagName==='A'?'arrow':null);
    if(!name || !urls[name])return;
    el.style.setProperty('--bf-link-icon',urls[name]);el.classList.add('bf-has-icon');
  }
  const selector='.bf a,.bf button,.driver-appbar-menu a,.driver-quick-nav a,.bf-ui .auth-card a,[data-bf-icon]';
  function scan(root){if(root.nodeType!==1)return;if(root.matches(selector))decorate(root);root.querySelectorAll(selector).forEach(decorate);}
  scan(document.body);
  // Calendar dialogs and scanner/POD fragments are inserted after page load.
  new MutationObserver(records=>{
    const roots=new Set();
    for(const r of records){if(r.target.nodeType===1)roots.add(r.target);else if(r.target.parentElement)roots.add(r.target.parentElement);for(const n of r.addedNodes)if(n.nodeType===1)roots.add(n);}
    roots.forEach(scan);
  }).observe(document.body,{childList:true,subtree:true,characterData:true});
})();
