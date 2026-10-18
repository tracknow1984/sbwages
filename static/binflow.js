'use strict';
(() => {
  const root = document.querySelector('.bf');
  if (!root) return;
  root.querySelectorAll('[data-print]').forEach(button => button.addEventListener('click', () => window.print()));
  root.querySelectorAll('[data-autosubmit]').forEach(select => select.addEventListener('change', () => select.form.requestSubmit()));
  root.querySelectorAll('[data-select-all]').forEach(control => control.addEventListener('change', () => {
    [...control.form.querySelectorAll('input[name="bin"]')].forEach((box, i) => { box.checked = control.checked && i < 200; });
  }));
  const size = root.querySelector('[data-size-select]');
  if (size) size.addEventListener('change', () => { root.querySelector('[data-default-tare]').value = size.selectedOptions[0].dataset.tare; });
  function updateCounts() {
    ['collect', 'deliver'].forEach(name => {
      const count = root.querySelectorAll('input[name="' + name + '"]:checked').length;
      const target = root.querySelector('[data-selected-count="' + name + '"]');
      if (target) target.textContent = count + ' selected';
    });
  }
  root.querySelectorAll('[data-group]').forEach(group => {
    const boxes = [...group.querySelectorAll('input[type="checkbox"]')];
    const quantity = group.querySelector('[data-quantity]');
    quantity.addEventListener('input', () => {
      const count = Math.max(0, Math.min(boxes.length, Number(quantity.value) || 0));
      boxes.forEach((box, i) => { box.checked = i < count; });
      updateCounts();
    });
    boxes.forEach(box => box.addEventListener('change', () => { quantity.value = boxes.filter(b => b.checked).length; updateCounts(); }));
  });
  root.querySelectorAll('[data-confirm-scan]').forEach(button => {
    const form = button.form;
    const input = form.querySelector('[data-manifest-scan]');
    const confirm = () => {
      let code = input.value.trim();
      try { if (/^https?:\/\//.test(code)) code = new URL(code).pathname.split('/').filter(Boolean).pop(); } catch (_) {}
      const box = [...form.querySelectorAll('input[name="confirmed"]')].find(b => b.dataset.code.toUpperCase() === code.toUpperCase() || b.dataset.token === code);
      const result = form.querySelector('[data-scan-result]');
      if (box) { box.checked = true; result.textContent = box.dataset.code + ' confirmed.'; input.value = ''; }
      else result.textContent = 'This bin is not on this manifest. Check the bin ID.';
      input.focus();
    };
    button.addEventListener('click', confirm);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); confirm(); } });
  });
  root.querySelectorAll('.bf-weigh-form').forEach(form => {
    const gross = form.querySelector('[data-gross]'), tare = form.querySelector('[data-tare]'), output = form.querySelector('[data-net]');
    const calc = () => {
      const value = Number(gross.value) - Number(tare.value);
      output.textContent = gross.value === '' ? '— kg' : value < 0 ? 'Check gross / tare' : value.toLocaleString('en-AU', {maximumFractionDigits:3}) + ' kg';
    };
    [gross, tare].forEach(input => input.addEventListener('input', calc));
  });
  root.querySelectorAll('[data-signature]').forEach(container => {
    const canvas = container.querySelector('canvas'), ctx = canvas.getContext('2d'), hidden = container.querySelector('input');
    let strokes = [], active = null;
    const point = e => { const r = canvas.getBoundingClientRect(); return [Math.max(0,Math.min(600,(e.clientX-r.left)*600/r.width)), Math.max(0,Math.min(200,(e.clientY-r.top)*200/r.height))]; };
    function draw() {
      ctx.clearRect(0,0,600,200); ctx.strokeStyle = '#172e4d'; ctx.lineWidth = 2.5; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      strokes.forEach(stroke => { ctx.beginPath(); stroke.forEach((p,i) => i ? ctx.lineTo(...p) : ctx.moveTo(...p)); ctx.stroke(); });
      hidden.value = JSON.stringify(strokes.filter(s => s.length > 1));
    }
    canvas.addEventListener('pointerdown', e => { e.preventDefault(); if (strokes.length >= 100) return; active = [point(e)]; strokes.push(active); canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener('pointermove', e => { if (!active) return; e.preventDefault(); if (active.length < 3000 && strokes.reduce((n,s) => n+s.length,0)<6000) active.push(point(e)); draw(); });
    ['pointerup','pointercancel'].forEach(event => canvas.addEventListener(event, () => { active=null; draw(); }));
    container.querySelector('[data-clear-signature]').addEventListener('click', () => { strokes=[]; active=null; draw(); });
    const select = container.closest('form').querySelector('[data-confirmation]');
    const update = () => { container.hidden = select.value !== 'signed'; };
    select.addEventListener('change', update); update();
  });
  root.querySelectorAll('form[method="post"]').forEach(form => form.addEventListener('submit', () => {
    // Disable only the submit button; keep all input values in the request.
    const button = form.querySelector('button[type="submit"],button:not([type])');
    if (button) { setTimeout(() => { button.disabled = true; }, 0); }
  }));
})();
