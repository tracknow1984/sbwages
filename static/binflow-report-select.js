'use strict';
document.querySelectorAll('[data-check-table]').forEach(toggle=>{
  const boxes=Array.from(toggle.closest('table').querySelectorAll('input[name=selected]'));
  toggle.addEventListener('change',()=>boxes.forEach(box=>{box.checked=toggle.checked;}));
  boxes.forEach(box=>box.addEventListener('change',()=>{
    toggle.checked=boxes.every(item=>item.checked);
    toggle.indeterminate=!toggle.checked && boxes.some(item=>item.checked);
  }));
});
