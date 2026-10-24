'use strict';
(() => {
 const form=document.querySelector('[data-bale-create]'); if(!form)return;
 const input=form.querySelector('[data-source-code]'),feedback=form.querySelector('[data-source-feedback]'),list=form.querySelector('[data-source-list]'),finish=form.querySelector('[data-bale-finish]');
 const warehouse=form.elements.warehouse_id,material=form.elements.material_id, sources=new Map(); let busy=false;
 const video=form.querySelector('video'),start=form.querySelector('[data-camera-start]'),stop=form.querySelector('[data-camera-stop]');let stream=null,timer=null,generation=0,opening=false,last='',lastAt=0;
 function say(text,error=false){feedback.textContent=text;feedback.style.color=error?'#a02828':'';}
 function render(){list.replaceChildren();for(const s of sources.values()){
  const row=document.createElement('div');row.className='bf-bale-row';const label=document.createElement('span');label.textContent=`${s.code} · ${s.material} · ${s.net_g/1000} kg`;
  row.append(label);for(const [name,value] of [['source_code',s.code],['source_item',s.item_id]]){const h=document.createElement('input');h.type='hidden';h.name=name;h.value=value;row.append(h);}
  const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='Remove';remove.setAttribute('aria-label',`Remove ${s.code}`);remove.onclick=()=>{sources.delete(s.code);render();};row.append(remove);list.append(row);
 }finish.hidden=!sources.size;warehouse.disabled=material.disabled=!!sources.size;
 // Disabled selects are mirrored so the server still receives their values.
 form.querySelectorAll('[data-context-copy]').forEach(e=>e.remove());if(sources.size)for(const select of [warehouse,material]){const h=document.createElement('input');h.type='hidden';h.name=select.name;h.value=select.value;h.dataset.contextCopy='';form.append(h);}
 }
 async function scan(code){if(busy||!code.trim())return;if(!material.value){say('Choose a material classification first.',true);return;}busy=true;const data=new FormData();data.set('csrf',form.elements.csrf.value);data.set('warehouse_id',warehouse.value);data.set('material_id',material.value);data.set('code',code);
 try{const response=await fetch(form.dataset.scanUrl,{method:'POST',body:data,headers:{Accept:'application/json'}});if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Refresh and sign in again before scanning.');const result=await response.json();if(!response.ok||!result.ok)throw Error(result.message||'Unable to check bin.');const duplicate=sources.has(result.code);sources.set(result.code,result);render();say(`${result.code} ${duplicate?'already scanned':'added'} · ${sources.size} bin(s) ready.`);input.value='';}catch(e){say(e.message||'Connection lost. Please scan again.',true);}finally{busy=false;}}
 form.querySelector('[data-add-source]').onclick=()=>scan(input.value);input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();scan(input.value);}});
 form.addEventListener('submit',e=>{if(busy||!sources.size){e.preventDefault();say('Finish scanning source bins first.',true);}else stopCamera();});
 function stopCamera(){generation++;clearTimeout(timer);stream?.getTracks().forEach(t=>t.stop());stream=null;video.srcObject=null;video.hidden=stop.hidden=true;start.hidden=false;}
 const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d',{willReadFrequently:true});
 function frame(){if(!stream)return;if(!busy&&video.readyState>=2&&video.videoWidth){const scale=Math.min(1,720/video.videoWidth);canvas.width=video.videoWidth*scale;canvas.height=video.videoHeight*scale;ctx.drawImage(video,0,0,canvas.width,canvas.height);const p=ctx.getImageData(0,0,canvas.width,canvas.height),q=window.jsQR(p.data,p.width,p.height);if(q&&(q.data!==last||Date.now()-lastAt>5000)){last=q.data;lastAt=Date.now();scan(q.data);}}timer=setTimeout(frame,250);}
 start.onclick=async()=>{if(opening||stream)return;if(!material.value){say('Choose a material classification first.',true);return;}if(!navigator.mediaDevices?.getUserMedia||!window.jsQR){say('Camera unavailable. Use the bin ID field.',true);return;}opening=true;const gen=generation;try{const camera=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});if(gen!==generation||document.hidden){camera.getTracks().forEach(t=>t.stop());return;}stream=camera;video.srcObject=stream;video.hidden=stop.hidden=false;start.hidden=true;await video.play();frame();}catch(e){stopCamera();say('Allow camera access or enter the bin ID below.',true);}finally{opening=false;}};
 stop.onclick=stopCamera;window.addEventListener('pagehide',stopCamera);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopCamera();});
})();
