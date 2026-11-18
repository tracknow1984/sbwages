'use strict';
document.querySelectorAll('[data-check-table]').forEach(toggle=>{
  const boxes=Array.from(toggle.closest('table').querySelectorAll('input[name=selected]'));
  toggle.addEventListener('change',()=>boxes.forEach(box=>{box.checked=toggle.checked;}));
  boxes.forEach(box=>box.addEventListener('change',()=>{
    toggle.checked=boxes.every(item=>item.checked);
    toggle.indeterminate=!toggle.checked && boxes.some(item=>item.checked);
  }));
});

const selectionCount = document.querySelector('[data-selection-count]');
if (selectionCount) {
  const form = selectionCount.closest('form');
  function updateCount() {
    const count = Array.from(form.elements).filter(input => input.name === 'selected' && input.checked).length;
    selectionCount.textContent = count + ' selected';
    form.querySelectorAll('button[name="format"]').forEach(button => { button.disabled = count === 0; });
  }
  document.addEventListener('change', event => {
    if (event.target.matches('input[name="selected"], [data-check-table]')) updateCount();
  });
  window.addEventListener('pageshow', updateCount);
  updateCount();
}
