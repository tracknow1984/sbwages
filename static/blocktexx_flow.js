window.BlocktexxFlow=(()=>{
  const W=1180,H=780,BW=230,BH=112;
  const stages=[
    ['dispatch','01','Depot dispatch','Truck departs with empty bins.','collection',30,90],
    ['collect','02','Customer collections','Switch empty bins for full bins\nat each pickup location.','collection',325,90],
    ['weigh','03','Return & weigh','Bring stock back to the depot.\nWeigh and record intake.','collection',620,90],
    ['decomm','04','Decomm partner','Deliver material for\ndecommissioning.','collection',915,90],
    ['bale','05','Return & bale','Collect decommissioned clothing.\nReturn to depot for baling.','transfer',915,320],
    ['interstate','06','Interstate depot','Store bales until enough stock\nis ready for a full B-double.','transfer',620,320],
    ['linehaul','07','B-double to QLD','Move the consolidated load\nto Threadtexx in Queensland.','transfer',325,320],
    ['shred','08','Threadtexx','Shred the material.\nPrepare stock for production.','transfer',30,320],
    ['decision','09','Production timing','Choose the next destination\nbased on production demand.','decision',30,555],
    ['storage','10','North Maclean','Hold shredded stock in storage\nuntil production is required.','storage',400,555],
    ['production','11','Blocktexx Loganholme','Deliver shredded stock\nfor production.','production',915,555]
  ];
  // Endpoints remain attached to the same process stages when the layout moves.
  const edges=[
    ['dispatch','collect','r','l',''],['collect','weigh','r','l',''],['weigh','decomm','r','l',''],
    ['decomm','bale','b','t','Collect & return'],['bale','interstate','l','r',''],['interstate','linehaul','l','r','Full load ready'],['linehaul','shred','l','r',''],
    ['shred','decision','b','t','After shredding'],['decision','storage','r','l','Hold stock'],
    ['storage','production','r','l','When production is required'],['decision','production','b','b','Ready for production now','bottom']
  ];
  const captions=[
    'The local truck leaves the depot carrying empty bins for customer switch-outs.',
    'At each pickup location, empty bins are delivered and full bins are collected.',
    'Collected material returns to the depot, where stock is weighed and intake is recorded.',
    'The stock is delivered to decommissioning partners for decommissioning.',
    'Decommissioned clothing is collected and brought back to the depot for baling.',
    'Bales are held at the interstate depot until there is enough stock for a full B-double.',
    'The consolidated B-double load travels to Threadtexx in Queensland.',
    'Threadtexx shreds the material, preparing it for the next production stage.',
    'After shredding, material follows one of two pathways based on production demand.',
    'If production is not yet required, North Maclean holds the stock and releases it when needed.',
    'Material goes to Blocktexx Loganholme either directly from Threadtexx or from North Maclean storage.'
  ];
  const svgNS='http://www.w3.org/2000/svg';
  const svg=(tag,attrs={},text)=>{const n=document.createElementNS(svgNS,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!=null)n.textContent=text;return n;};
  const html=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const clamp=(v,max)=>Math.max(8,Math.min(max-8,v));
  function positions(saved={}){return Object.fromEntries(stages.map(([id,,,,,x,y])=>[id,{x:Number.isFinite(saved[id]?.x)?clamp(saved[id].x,W-BW):x,y:Number.isFinite(saved[id]?.y)?clamp(saved[id].y,H-BH):y}]));}
  function anchor(p,side){return {x:p.x+(side==='l'?0:side==='r'?BW:BW/2),y:p.y+(side==='t'?0:side==='b'?BH:BH/2)};}
  function geometry(edge,layout){
    const [from,to,out,into]=edge,a=anchor(layout[from],out),b=anchor(layout[to],into);
    if(edge[5]==='bottom'){
      const y=Math.min(H-22,Math.max(a.y,b.y)+65);
      return {d:`M ${a.x} ${a.y} C ${a.x} ${y}, ${a.x} ${y}, ${a.x+30} ${y} L ${b.x-30} ${y} C ${b.x} ${y}, ${b.x} ${y}, ${b.x} ${b.y}`,x:(a.x+b.x)/2,y:y-10};
    }
    const horizontal=['l','r'].includes(out),gap=horizontal?Math.max(35,Math.abs(b.x-a.x)*.45):Math.max(35,Math.abs(b.y-a.y)*.45);
    const offset=side=>({x:side==='r'?gap:side==='l'?-gap:0,y:side==='b'?gap:side==='t'?-gap:0});
    const u=offset(out),v=offset(into);
    return {d:`M ${a.x} ${a.y} C ${a.x+u.x} ${a.y+u.y}, ${b.x+v.x} ${b.y+v.y}, ${b.x} ${b.y}`,x:(a.x+b.x)/2+(horizontal?0:65),y:horizontal&&Math.abs(b.x-a.x)<150?Math.min(layout[from].y,layout[to].y)-14:(a.y+b.y)/2-(horizontal?12:0)};
  }
  function render(root,model,onChange){
    root._dispose?.();root.replaceChildren();let timer=null,index=-1,drag=null;
    let layout=positions(model.process_flow_layout);
    const header=html('div',null,'bx-flow-header'),intro=html('div');
    intro.append(html('p','SB EMPIRE × BLOCKTEXX','bx-flow-eyebrow'),html('h2','From collection to production'),html('p','One connected journey. Two pathways after shredding.','bx-flow-subtitle'));
    const controls=html('div',null,'bx-flow-controls');
    const button=(text,fn)=>{const b=html('button',text,'secondary');b.type='button';b.onclick=fn;controls.append(b);return b;};
    const play=button('Play walkthrough',()=>{if(timer){stop();return;}index=-1;advance();timer=setInterval(advance,5000);play.textContent='Pause walkthrough';});
    button('Reset layout',()=>{stop();index=-1;nodeElements.forEach(({g})=>g.classList.remove('is-active'));edgeElements.forEach(({path})=>path.classList.remove('is-active'));caption.textContent='Drag any stage to arrange the flow. Arrows stay connected.';layout=positions();draw();persist();status.textContent='Default layout restored. Select Save layout to keep it.';});
    button('Save layout',()=>{persist();document.getElementById('bx-save')?.click();});
    const full=button('Presentation view',async()=>{try{if(document.fullscreenElement===root)await document.exitFullscreen();else await root.requestFullscreen();}catch{status.textContent='Use your browser’s full-screen control to present this page.';}});
    header.append(intro,controls);root.append(header);
    const legend=html('div',null,'bx-flow-legend');[['collection','Collection & decommissioning'],['transfer','Baling, linehaul & shredding'],['storage','Storage'],['production','Production']].forEach(([c,t])=>legend.append(html('span',t,'bx-flow-key '+c)));root.append(legend);
    const frame=html('div',null,'bx-flow-frame'),canvas=svg('svg',{viewBox:`0 0 ${W} ${H}`,class:'bx-flow-canvas','aria-label':'Blocktexx transport, decommissioning, storage and production process'});
    canvas.append(svg('title',{},'Collection to production — draggable process stages'),svg('desc',{},'Follow stages 1 to 9, then either go directly to production or hold stock in North Maclean before production. Drag a stage to reposition it, or focus it and use arrow keys.'));
    const defs=svg('defs'),marker=svg('marker',{id:'bx-flow-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#8195ac'}));defs.append(marker);canvas.append(defs);
    const lines=svg('g',{'aria-hidden':'true'}),nodes=svg('g');canvas.append(lines,nodes);frame.append(canvas);root.append(frame);
    const caption=html('p','Drag any stage to arrange the flow. Arrows stay connected.','bx-flow-caption');caption.setAttribute('aria-live','polite');root.append(caption);
    const status=html('p','Layout changes only the presentation; planner bookings and costs stay as entered.','bx-flow-status');status.setAttribute('role','status');root.append(status);
    const edgeElements=edges.map(edge=>{const path=svg('path',{class:'bx-flow-edge','marker-end':'url(#bx-flow-arrow)'}),text=svg('text',{class:'bx-flow-edge-label','text-anchor':'middle'},edge[4]);lines.append(path,text);return {edge,path,text};});
    const nodeElements=stages.map(([id,number,title,body,category])=>{
      const g=svg('g',{class:'bx-flow-node '+category,tabindex:0,role:'button','aria-label':number+'. '+title+'. '+body.replace('\n',' ')+' Drag or use arrow keys to move.'});
      g.append(svg('rect',{width:BW,height:BH,rx:12,class:'bx-flow-node-bg'}),svg('rect',{x:0,y:15,width:4,height:82,rx:2,class:'bx-flow-accent'}),svg('text',{x:18,y:25,class:'bx-flow-number'},number),svg('text',{x:48,y:25,class:'bx-flow-category'},category==='collection'?'LOCAL NETWORK':category==='transfer'?'CONSOLIDATION':category==='decision'?'DESTINATION':category.toUpperCase()),svg('text',{x:18,y:53,class:'bx-flow-title'},title));
      const copy=svg('text',{x:18,y:77,class:'bx-flow-copy'});body.split('\n').forEach((line,i)=>copy.append(svg('tspan',{x:18,dy:i?18:0},line)));g.append(copy);nodes.append(g);
      g.addEventListener('pointerdown',event=>{if(event.button!==0)return;stop();const p=point(event);if(!p)return;drag={id,dx:p.x-layout[id].x,dy:p.y-layout[id].y,moved:false};g.setPointerCapture(event.pointerId);g.classList.add('is-dragging');event.preventDefault();});
      g.addEventListener('pointermove',event=>{if(drag?.id!==id)return;const p=point(event);if(!p)return;layout[id]={x:clamp(p.x-drag.dx,W-BW),y:clamp(p.y-drag.dy,H-BH)};drag.moved=true;draw();});
      const end=()=>{if(drag?.id!==id)return;const moved=drag.moved;drag=null;g.classList.remove('is-dragging');if(moved){persist();status.textContent='Layout changed — select Save layout to keep it.';}};
      g.addEventListener('pointerup',end);g.addEventListener('pointercancel',end);g.addEventListener('lostpointercapture',end);
      g.addEventListener('keydown',event=>{const delta={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];if(!delta)return;event.preventDefault();stop();const step=event.shiftKey?20:5;layout[id]={x:clamp(layout[id].x+delta[0]*step,W-BW),y:clamp(layout[id].y+delta[1]*step,H-BH)};draw();persist();status.textContent='Layout changed — select Save layout to keep it.';});
      return {id,g};
    });
    function point(event){const matrix=canvas.getScreenCTM();if(!matrix)return null;return new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());}
    function draw(){nodeElements.forEach(({id,g})=>g.setAttribute('transform',`translate(${layout[id].x} ${layout[id].y})`));edgeElements.forEach(({edge,path,text})=>{const p=geometry(edge,layout);path.setAttribute('d',p.d);text.setAttribute('x',p.x);text.setAttribute('y',p.y);});}
    function persist(){model.process_flow_layout=Object.fromEntries(Object.entries(layout).map(([id,p])=>[id,{x:Math.round(p.x),y:Math.round(p.y)}]));onChange();}
    function stop(){clearInterval(timer);timer=null;play.textContent='Play walkthrough';}
    function advance(){index++;if(index>=stages.length){stop();index=-1;caption.textContent='Journey complete. Replay the walkthrough or rearrange the stages.';}else caption.textContent=captions[index];nodeElements.forEach(({g},i)=>g.classList.toggle('is-active',i===index));edgeElements.forEach(({edge,path})=>path.classList.toggle('is-active',index>=0&&edge[1]===stages[index][0]));}
    const fullscreen=()=>{full.textContent=document.fullscreenElement===root?'Exit presentation':'Presentation view';};document.addEventListener('fullscreenchange',fullscreen);
    const saveStatus=document.getElementById('bx-save-status');
    const observer=saveStatus?new MutationObserver(()=>{status.textContent=saveStatus.textContent;}):null;
    observer?.observe(saveStatus,{childList:true,characterData:true,subtree:true});
    root._dispose=()=>{stop();observer?.disconnect();document.removeEventListener('fullscreenchange',fullscreen);};
    root._pause=stop;draw();
  }
  return {render,positions,geometry,stages,edges};
})();
