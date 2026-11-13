'use strict';
(() => {
 const form=document.querySelector('[data-bale-create]'); if(!form)return;
 const feedback=form.querySelector('[data-source-feedback]'),list=form.querySelector('[data-source-list]'),finish=form.querySelector('[data-bale-finish]');
 const warehouse=form.elements.warehouse_id,material=form.elements.material_id, key=form.elements.request_key.value;
 const limit=Number(form.dataset.limitG),max=Number(form.dataset.maxG),meter=form.querySelector('.bf-bale-meter');
 const overrideBox=form.querySelector('[data-override]'),legacyOverride=form.querySelector('[data-draft-override]');
 const kg=grams=>(grams/1000).toLocaleString('en-AU',{maximumFractionDigits:3})+' kg';
 let proposal=null;
 let sources=JSON.parse(form.querySelector('[data-saved-sources]').textContent), busy=false;
 const video=form.querySelector('video'),start=form.querySelector('[data-camera-start]'),stop=form.querySelector('[data-camera-stop]');let stream=null,timer=null,generation=0,opening=false,last='',lastAt=0;
 function say(text,error=false){feedback.textContent=text;feedback.style.color=error?'#a02828':'';}
 const stock=form.querySelector('[data-ready-stock]');
 let stockVersion=0;
 function filterStock() {
   for (const button of stock.querySelectorAll('[data-select-bin]')) {
     const group=button.closest('[data-stock-material]');
     button.disabled=busy || !!proposal || (material.value!=='' && material.value!==group.dataset.stockMaterial);
   }
 }
 async function refreshStock() {
   const version=++stockVersion;
   const url=new URL(form.dataset.stockUrl,location.origin);
   url.searchParams.set('stock','1'); url.searchParams.set('warehouse',warehouse.value);
   try {
     const response=await fetch(url,{cache:'no-store'});
     if(!response.ok || response.redirected) throw Error();
     const html=await response.text();
     if(version===stockVersion) { stock.innerHTML=html; filterStock(); }
   } catch {
     if(version===stockVersion) stock.textContent='Stock could not be loaded. Use Refresh stock to try again.';
   }
 }
 warehouse.addEventListener('change',()=>{proposal=null; stock.replaceChildren(); render(); refreshStock();});
 material.addEventListener('change',()=>{proposal=null;render();});
 form.querySelector('[data-refresh-stock]').onclick=refreshStock;
 stock.addEventListener('click',event=>{
   const button=event.target.closest('[data-select-bin]');
   if(button && !button.disabled) scan(button.dataset.selectBin,null,null,button.dataset.stockItem);
 });
 function render() {
   list.replaceChildren();
   for (const source of sources) {
     const row=document.createElement('div');
     row.className='bf-bale-row';
     const label=document.createElement('span');
     label.textContent=source.code+' · '+source.material+' · '+source.net_g/1000+' kg';
     row.append(label);
     for (const [name,value] of [['source_code',source.code],['source_item',source.item_id]]) {
       const field=document.createElement('input');
       field.type='hidden'; field.name=name; field.value=value; row.append(field);
     }
     const undo=document.createElement('button');
     undo.type='button'; undo.className='secondary'; undo.textContent='Undo scan'; undo.disabled=busy;
     undo.setAttribute('aria-label','Undo scan for '+source.code);
     undo.onclick=()=>{
       stopCamera();
       if (window.confirm('Confirm all contents are back in '+source.code+' before undoing this scan.')) scan(source.code,source.item_id);
     };
     row.append(undo); list.append(row);
   }
   const netGrams=sources.reduce((total,source)=>total+source.net_g,0);
   if(sources.length) material.value=String(sources[0].material_id);
   form.querySelector('[data-bale-total]').textContent=kg(netGrams);
   const over=netGrams>limit,tooHeavy=netGrams>max;
   meter.toggleAttribute('data-over',over);
   meter.querySelector('progress').max=over?max:limit;
   meter.querySelector('progress').value=netGrams;
   form.querySelector('[data-remaining]').textContent=tooHeavy?'Over the maximum. Remove a source before creating this bale.':
     over?kg(netGrams-limit)+' over target · '+kg(max-netGrams)+' to the maximum':
     kg(limit-netGrams)+' to the 300 kg limit · override available up to '+kg(max);
   const priorApproval=Math.max(0,...sources.map(source=>source.override_total_g||0));
   legacyOverride.hidden=!over || tooHeavy || priorApproval>=netGrams;
   const draftConfirmation=legacyOverride.querySelector('input');
   draftConfirmation.disabled=legacyOverride.hidden;
   draftConfirmation.required=!legacyOverride.hidden;
   if(draftConfirmation.value!==String(netGrams)) draftConfirmation.checked=false;
   draftConfirmation.value=String(netGrams);
   overrideBox.hidden=!proposal;
   if(proposal) form.querySelector('[data-override-message]').textContent=proposal.message+' Approve this weight before emptying the bin.';
   finish.hidden=!sources.length;
   warehouse.disabled=material.disabled=busy || !!sources.length;
   filterStock();
   form.querySelector('[type=submit]').disabled=busy || !!proposal || tooHeavy;
   form.querySelectorAll('[data-context-copy]').forEach(e=>e.remove());
   if (sources.length) for (const select of [warehouse,material]) {
     const field=document.createElement('input');
     field.type='hidden'; field.name=select.name; field.value=select.value; field.dataset.contextCopy='';
     form.append(field);
   }
 }
 async function scan(code,undoItem=null,approval=null,expectedItem=null) {
   if (busy || !code.trim()) return;
   proposal=null;
   busy=true;
   render();
   const data=new FormData();
   data.set('csrf',form.elements.csrf.value); data.set('request_key',key);
   data.set('warehouse_id',warehouse.value); data.set('material_id',material.value); data.set('code',code);
   if(expectedItem) data.set('expected_item',expectedItem);
   if(approval) {data.set('override_total_g',approval.projected_g);data.set('override_item_id',approval.item_id);}
   if (undoItem!==null) { data.set('action','undo'); data.set('source_item',undoItem); }
   try {
     const address=new URL(location.href);
     address.searchParams.set('draft',key);
     history.replaceState(null,'',address);
     const response=await fetch(form.dataset.scanUrl,{method:'POST',body:data,headers:{Accept:'application/json'}});
     if (!response.headers.get('content-type')?.includes('application/json')) throw Error('Sign in and reopen this bale to check your saved scans.');
     const result=await response.json();
     if (Array.isArray(result.sources)) sources=result.sources;
     if(result.override_required) {
       stopCamera(); proposal=result; say('Capacity override required.',true); return;
     }
     if (!response.ok || !result.ok) throw Error(result.message || 'Unable to save the scan.');
     say(undoItem!==null ? 'Scan reversed. Bin returned to weighed stock.' :
       result.code+(result.duplicate?' was already scanned for this bale.':' is empty and available for its next job.'));

   } catch(error) { stopCamera(); say(error.message || 'Connection lost. Reopen this bale to check the saved scans.',true); }
   finally { await refreshStock(); busy=false; render(); }
 }
 render();
 form.querySelector('[data-approve]').onclick=()=>{if(proposal) scan(proposal.code,null,proposal);};
 form.querySelector('[data-decline]').onclick=()=>{proposal=null;render();say('Bin kept full. Scan another bin or finish this bale.');};
 form.addEventListener('submit',e=>{if(busy||proposal||!sources.length){e.preventDefault();say('Finish scanning source bins first.',true);}else stopCamera();});
 function stopCamera(){generation++;clearTimeout(timer);stream?.getTracks().forEach(t=>t.stop());stream=null;video.srcObject=null;video.hidden=stop.hidden=true;start.hidden=false;}
 const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d',{willReadFrequently:true});
 function frame(){if(!stream)return;if(!busy&&video.readyState>=2&&video.videoWidth){const scale=Math.min(1,720/video.videoWidth);canvas.width=video.videoWidth*scale;canvas.height=video.videoHeight*scale;ctx.drawImage(video,0,0,canvas.width,canvas.height);const p=ctx.getImageData(0,0,canvas.width,canvas.height),q=window.jsQR(p.data,p.width,p.height);if(q&&(q.data!==last||Date.now()-lastAt>5000)){last=q.data;lastAt=Date.now();scan(q.data);}}timer=setTimeout(frame,250);}
 start.onclick=async()=>{if(opening||stream)return;if(!navigator.mediaDevices?.getUserMedia||!window.jsQR){say('Camera unavailable. Select an available bin below.',true);return;}opening=true;const gen=generation;try{const camera=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});if(gen!==generation||document.hidden){camera.getTracks().forEach(t=>t.stop());return;}stream=camera;video.srcObject=stream;video.hidden=stop.hidden=false;start.hidden=true;await video.play();frame();}catch(e){stopCamera();say('Allow camera access or select an available bin below.',true);}finally{opening=false;}};
 stop.onclick=stopCamera;window.addEventListener('pagehide',stopCamera);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopCamera();});
})();
