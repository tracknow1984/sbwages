'use strict';
(() => {
 const form=document.querySelector('[data-bale-create]'); if(!form)return;
 const input=form.querySelector('[data-source-code]'),feedback=form.querySelector('[data-source-feedback]'),list=form.querySelector('[data-source-list]'),finish=form.querySelector('[data-bale-finish]');
 const warehouse=form.elements.warehouse_id,material=form.elements.material_id, key=form.elements.request_key.value;
 let sources=JSON.parse(form.querySelector('[data-saved-sources]').textContent), busy=false;
 const video=form.querySelector('video'),start=form.querySelector('[data-camera-start]'),stop=form.querySelector('[data-camera-stop]');let stream=null,timer=null,generation=0,opening=false,last='',lastAt=0;
 function say(text,error=false){feedback.textContent=text;feedback.style.color=error?'#a02828':'';}
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
   finish.hidden=!sources.length;
   warehouse.disabled=material.disabled=busy || !!sources.length;
   form.querySelector('[type=submit]').disabled=busy;
   form.querySelectorAll('[data-context-copy]').forEach(e=>e.remove());
   if (sources.length) for (const select of [warehouse,material]) {
     const field=document.createElement('input');
     field.type='hidden'; field.name=select.name; field.value=select.value; field.dataset.contextCopy='';
     form.append(field);
   }
 }
 async function scan(code,undoItem=null) {
   if (busy || !code.trim()) return;
   if (!material.value) { say('Choose a material first.',true); return; }
   busy=true;
   render();
   const data=new FormData();
   data.set('csrf',form.elements.csrf.value); data.set('request_key',key);
   data.set('warehouse_id',warehouse.value); data.set('material_id',material.value); data.set('code',code);
   if (undoItem!==null) { data.set('action','undo'); data.set('source_item',undoItem); }
   try {
     const address=new URL(location.href);
     address.searchParams.set('draft',key);
     history.replaceState(null,'',address);
     const response=await fetch(form.dataset.scanUrl,{method:'POST',body:data,headers:{Accept:'application/json'}});
     if (!response.headers.get('content-type')?.includes('application/json')) throw Error('Sign in and reopen this bale to check your saved scans.');
     const result=await response.json();
     if (!response.ok || !result.ok) throw Error(result.message || 'Unable to save the scan.');
     sources=result.sources;
     say(undoItem!==null ? 'Scan reversed. Bin returned to weighed stock.' :
       result.code+(result.duplicate?' was already scanned for this bale.':' is empty and available for its next job.'));
     input.value='';
   } catch(error) { say(error.message || 'Connection lost. Reopen this bale to check the saved scans.',true); }
   finally { busy=false; render(); }
 }
 render();
 form.querySelector('[data-add-source]').onclick=()=>scan(input.value);
 input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();scan(input.value);}});
 form.addEventListener('submit',e=>{if(busy||!sources.length){e.preventDefault();say('Finish scanning source bins first.',true);}else stopCamera();});
 function stopCamera(){generation++;clearTimeout(timer);stream?.getTracks().forEach(t=>t.stop());stream=null;video.srcObject=null;video.hidden=stop.hidden=true;start.hidden=false;}
 const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d',{willReadFrequently:true});
 function frame(){if(!stream)return;if(!busy&&video.readyState>=2&&video.videoWidth){const scale=Math.min(1,720/video.videoWidth);canvas.width=video.videoWidth*scale;canvas.height=video.videoHeight*scale;ctx.drawImage(video,0,0,canvas.width,canvas.height);const p=ctx.getImageData(0,0,canvas.width,canvas.height),q=window.jsQR(p.data,p.width,p.height);if(q&&(q.data!==last||Date.now()-lastAt>5000)){last=q.data;lastAt=Date.now();scan(q.data);}}timer=setTimeout(frame,250);}
 start.onclick=async()=>{if(opening||stream)return;if(!material.value){say('Choose a material classification first.',true);return;}if(!navigator.mediaDevices?.getUserMedia||!window.jsQR){say('Camera unavailable. Use the bin ID field.',true);return;}opening=true;const gen=generation;try{const camera=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});if(gen!==generation||document.hidden){camera.getTracks().forEach(t=>t.stop());return;}stream=camera;video.srcObject=stream;video.hidden=stop.hidden=false;start.hidden=true;await video.play();frame();}catch(e){stopCamera();say('Allow camera access or enter the bin ID below.',true);}finally{opening=false;}};
 stop.onclick=stopCamera;window.addEventListener('pagehide',stopCamera);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopCamera();});
})();
