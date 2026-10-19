'use strict';
(() => {
  const zone=()=>Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.querySelectorAll('[data-device-now]').forEach(node=>{
    node.textContent=new Date().toLocaleString('en-AU',{weekday:'long',day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'})+' · '+zone();
  });
  document.querySelectorAll('[data-clock-form]').forEach(form=>form.addEventListener('submit',()=>{
    for(const [name,value] of Object.entries({device_at:new Date().toISOString(),device_zone:zone()})) {
      let input=form.elements.namedItem(name);
      if(!input){input=document.createElement('input');input.type='hidden';input.name=name;form.append(input);}
      input.value=value;
    }
  }));
})();
