'use strict';
(() => {
  const labels={moving:'Moving',stopped:'Stopped',current:'Current position',stale:'Last known position · stale',unavailable:'GPS position unavailable',unlinked:'GPS unit not linked'};
  const colours={moving:'#225bc0',stopped:'#168253',current:'#225bc0',stale:'#b7791f',unavailable:'#718096',unlinked:'#718096'};
  function hasPosition(unit) {
    return Number.isFinite(unit.latitude) && Number.isFinite(unit.longitude) && Math.abs(unit.latitude)<=90 && Math.abs(unit.longitude)<=180 && Number.isFinite(unit.position_time);
  }
  function statusFor(unit,epoch,offline=false,staleSeconds=300) {
    if (!hasPosition(unit)) return unit.status==='unlinked'?'unlinked':'unavailable';
    if (offline || !unit.fresh || epoch-unit.position_time>staleSeconds || unit.position_time-epoch>60) return 'stale';
    return unit.speed===null || !Number.isFinite(unit.speed)?'current':unit.speed>0?'moving':'stopped';
  }
  // Test the age/offline rules independently of a browser or live tracking account.
  if (typeof module!=='undefined' && module.exports) module.exports={hasPosition,statusFor};
  if (typeof document==='undefined') return;
  const root=document.querySelector('[data-fleet-tracking]');
  if (!root) return;
  const find=selector=>root.querySelector(selector);
  const message=find('[data-tracking-message]'),content=find('[data-tracking-content]'),empty=find('[data-tracking-empty]');
  const note=find('[data-tracking-note]'),list=find('[data-fleet-list]'),updated=find('[data-tracking-updated]');
  const refreshButton=find('[data-tracking-refresh]'),fitButton=find('[data-tracking-fit]'),fenceToggle=find('[data-tracking-fences]');
  const mapElement=find('[data-fleet-map]');
  const centres={QLD:[-22.4,144.7,5],NSW:[-32.2,147,6],VIC:[-37,144.5,6],SA:[-30.5,135,5],WA:[-25.5,122,4],TAS:[-42,146.5,7],ACT:[-35.4,149.1,10],NT:[-19.5,133.5,5]};
  let map,markers=new Map(),cards=new Map(),fences,firstFit=true,selected=null,snapshot=null,receivedAt=0,offline=false,busy=false,sessionEnded=false,tileFailed=false,lastStates='';
  const element=(tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
  const formatted=epoch=>new Date(epoch*1000).toLocaleString('en-AU',{timeZone:'Australia/Brisbane',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' AEST';
  function showNote() {
    const notes=[];
    if(tileFailed) notes.push('Map background is unavailable. Vehicle positions remain available in the list.');
    if(snapshot?.unlinked_count) notes.push(`${snapshot.unlinked_count} Wialon units need a truck link for state filtering and staff tracking. Use GPS settings to match them.`);
    note.textContent=notes.join(' ');note.hidden=!notes.length;
  }
  function createMap() {
    if(map || !window.L) return;
    const centre=centres[root.dataset.state]||[-25.5,134,4];
    map=window.L.map(mapElement,{scrollWheelZoom:false}).setView(centre.slice(0,2),centre[2]);
    // An origin-only referrer is set just for public map tiles. No API token or unit data is sent to the tile server.
    window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
      maxZoom:19,referrerPolicy:'origin',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).on('tileerror',()=>{tileFailed=true;showNote();}).addTo(map);
    fences=window.L.layerGroup().addTo(map);
    new ResizeObserver(()=>map.invalidateSize()).observe(mapElement);
  }
  function fitFleet() {
    if(!map) return;
    const points=Array.from(markers.values()).map(marker=>marker.getLatLng());
    if(points.length) map.fitBounds(window.L.latLngBounds(points),{padding:[40,40],maxZoom:14});
  }
  function select(key,focusMap=false) {
    selected=key;
    cards.forEach((row,id)=>row.node.classList.toggle('is-selected',id===key));
    const marker=markers.get(key);
    if(focusMap && marker) {map.setView(marker.getLatLng(),Math.max(map.getZoom(),13));marker.openPopup();}
  }
  function popup(unit,status) {
    const node=element('div',undefined,'bf-map-popup');
    node.append(element('strong',unit.name),element('span',labels[status]),element('span',unit.state||'Wialon unit · truck link needed'));
    if(unit.speed!==null) node.append(element('span',(status==='stale'?'Last reported speed: ':'Speed: ')+Math.round(unit.speed)+' km/h'));
    if(unit.position_time) node.append(element('span','GPS '+formatted(unit.position_time)));
    if(status!=='stale' && unit.places.length) node.append(element('span',unit.places.join(' / ')));
    const link=element('a','Open location ↗');link.href='https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(unit.latitude+','+unit.longitude);link.target='_blank';link.rel='noopener noreferrer';node.append(link);
    return node;
  }
  function clearFleet() {
    markers.forEach(marker=>marker.remove());markers.clear();fences?.clearLayers();cards.clear();list.replaceChildren();selected=null;firstFit=true;
  }
  function showFences() {
    if(!fences || !snapshot) return;
    fences.clearLayers();
    if(!fenceToggle.checked) return;
    snapshot.geofences.forEach(fence=>{
      const colour=fence.kind==='warehouse'?'#7544a3':'#537a9c';
      const label=(fence.client_name?fence.client_name+' · ':'')+fence.name+' · '+fence.radius_m+' m';
      window.L.circle([fence.latitude,fence.longitude],{radius:fence.radius_m,color:colour,weight:1,fillOpacity:.09})
        .bindTooltip(element('span',label)).addTo(fences);
    });
  }
  function renderUnits() {
    if(!snapshot?.connected) return;
    const epoch=snapshot.server_time+(performance.now()-receivedAt)/1000;
    const seen=new Set();let live=0,stale=0;
    snapshot.units.forEach(unit=>{
      seen.add(unit.key);
      const status=statusFor(unit,epoch,offline,snapshot.stale_seconds);
      if(['moving','stopped','current'].includes(status)) live++;if(status==='stale')stale++;
      let row=cards.get(unit.key);
      if(!row) {
        row={node:element('article',undefined,'bf-fleet-card'),name:element('strong'),meta:element('small'),status:element('span'),place:element('small'),time:element('small'),button:element('button','Show on map','secondary compact')};
        row.button.type='button';row.button.addEventListener('click',()=>select(unit.key,true));
        row.node.append(row.name,row.meta,row.status,row.place,row.time,row.button);list.append(row.node);cards.set(unit.key,row);
      }
      row.name.textContent=unit.name;
      row.meta.textContent=unit.truck_id?unit.state+(unit.unit_name?' · '+unit.unit_name:''):'Wialon unit · truck link needed';
      row.status.className='bf-map-status '+status;
      row.status.textContent=labels[status]+(status==='moving'?' · '+Math.round(unit.speed)+' km/h':'');
      row.time.textContent=unit.position_time?'GPS '+formatted(unit.position_time):'No GPS timestamp available';
      row.place.textContent=status!=='stale'&&unit.places.length?unit.places.join(' / '):'';
      row.button.hidden=!map||!hasPosition(unit);
      row.node.classList.toggle('is-selected',selected===unit.key);
      if(map && hasPosition(unit)) {
        let marker=markers.get(unit.key);
        if(!marker) {
          marker=window.L.circleMarker([unit.latitude,unit.longitude],{radius:9,weight:3,fillOpacity:.95}).addTo(map);
          marker.bindTooltip(element('span',unit.name));marker.bindPopup(popup(unit,status));
          marker.on('click',()=>select(unit.key));markers.set(unit.key,marker);
        } else {marker.setLatLng([unit.latitude,unit.longitude]);marker.setTooltipContent(element('span',unit.name));marker.setPopupContent(popup(unit,status));}
        marker.setStyle({color:status==='stale'?'#805619':'#ffffff',fillColor:colours[status],dashArray:status==='stale'?'3 2':null});
      } else if(markers.has(unit.key)) {markers.get(unit.key).remove();markers.delete(unit.key);}
    });
    cards.forEach((row,key)=>{if(!seen.has(key)){row.node.remove();cards.delete(key);}});
    markers.forEach((marker,key)=>{if(!seen.has(key)){marker.remove();markers.delete(key);}});
    if(!snapshot.units.length && !list.querySelector('[data-empty-fleet]')) {const msg=element('p',root.dataset.state?'No linked trucks in this state yet.':'No vehicles available yet.', 'bf-pad');msg.dataset.emptyFleet='true';list.append(msg);}
    if(snapshot.units.length) list.querySelector('[data-empty-fleet]')?.remove();
    const error=offline?'Connection interrupted — showing last known positions.':snapshot.error;
    message.textContent=error || `${live} current · ${stale} stale · ${snapshot.units.length} vehicles`;
    updated.textContent=snapshot.last_success?'Last successful sync '+formatted(snapshot.last_success):'Waiting for the first GPS sync';
    fitButton.hidden=!map||!markers.size;
    if(firstFit && markers.size){fitFleet();firstFit=false;}
    lastStates=snapshot.units.map(unit=>statusFor(unit,epoch,offline,snapshot.stale_seconds)).join('|');
  }
  async function refresh() {
    if(busy || document.hidden || sessionEnded) return;
    busy=true;refreshButton.disabled=true;
    const controller=new AbortController(),timeout=window.setTimeout(()=>controller.abort(),28000);
    try {
      const response=await fetch(root.dataset.trackingUrl,{headers:{Accept:'application/json'},cache:'no-store',redirect:'manual',signal:controller.signal});
      if(response.status===401 || response.status===403 || response.type==='opaqueredirect') {
        clearFleet();snapshot=null;content.hidden=true;empty.hidden=true;sessionEnded=true;message.textContent='Sign in again to view fleet tracking.';return;
      }
      if(!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Tracking unavailable');
      const data=await response.json();
      if(!Array.isArray(data.units)||!Array.isArray(data.geofences)||!Number.isFinite(data.server_time)) throw new Error('Invalid tracking data');
      snapshot=data;receivedAt=performance.now();offline=false;
      content.hidden=!data.connected;empty.hidden=data.connected;
      if(!data.connected) {clearFleet();message.textContent='Wialon is not connected yet.';fitButton.hidden=true;return;}
      createMap();
      if(!map) {tileFailed=true;mapElement.textContent='Map unavailable. Use the vehicle list to check positions.';}
      showNote();showFences();renderUnits();map?.invalidateSize();
    } catch(_) {
      offline=true;
      if(snapshot?.connected) renderUnits();else message.textContent='Unable to load tracking. Check your connection and tap Refresh.';
    } finally {window.clearTimeout(timeout);busy=false;refreshButton.disabled=sessionEnded;}
  }
  refreshButton.addEventListener('click',refresh);fitButton.addEventListener('click',fitFleet);fenceToggle.addEventListener('change',showFences);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden) refresh();});
  window.setInterval(refresh,30000);
  window.setInterval(()=>{
    if(!snapshot?.connected||document.hidden) return;
    const epoch=snapshot.server_time+(performance.now()-receivedAt)/1000;
    if(snapshot.units.map(unit=>statusFor(unit,epoch,offline,snapshot.stale_seconds)).join('|')!==lastStates) renderUnits();
  },5000);
  refresh();
})();
