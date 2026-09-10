const search = document.querySelector('#staff-search');
if (search) search.addEventListener('input', () => {
  const rows = [...document.querySelectorAll('#staff-table tbody tr')];
  let visible = 0;
  for (const row of rows) {
    row.hidden = !row.textContent.toLowerCase().includes(search.value.toLowerCase().trim());
    if (!row.hidden) visible++;
  }
  document.querySelector('#no-matches').classList.toggle('hidden', visible > 0);
});
const summary = document.querySelector('#timesheet-summary');
if (summary) {
  const dirty = new Set();
  const rows = [...document.querySelectorAll('.day-row')];
  const update = () => {
    const units = rows.reduce((total, row) => total + Number(row.dataset.units), 0);
    document.querySelector('#total-hours').textContent = (units / 100).toLocaleString('en-AU', {maximumFractionDigits: 2});
    document.querySelector('#total-pay').textContent = (Math.round(units * Number(summary.dataset.rate) / 100) / 100).toLocaleString('en-AU', {style: 'currency', currency: 'AUD'});
  };
  for (const row of rows) {
    const times = [...row.querySelectorAll('.shift-time')];
    const recalculate = () => {
      const minutes = value => { const [h, m] = value.split(':').map(Number); return h * 60 + m; };
      const valid = times.length === 2 && times.every(field => field.value);
      const duration = valid ? minutes(times[1].value) - minutes(times[0].value) : 0;
      row.dataset.units = String(duration > 0 ? Math.round(duration * 100 / 60) : 0);
      row.querySelector('.day-hours').textContent = duration > 0 ? String(Number(row.dataset.units) / 100) : '0';
      if (times[1]) times[1].setCustomValidity(valid && duration <= 0 ? 'Finish must be after start. Split overnight work across two dates.' : '');
      update();
    };
    row.querySelectorAll('input:not([type="hidden"]),textarea').forEach(field => field.addEventListener('input', () => {
      if (field.form) dirty.add(field.form.id);
      if (field.classList.contains('shift-time')) recalculate();
    }));
    if (times.some(field => field.value)) recalculate();
  }
  document.querySelectorAll('.day-form,.week-submit').forEach(form => form.addEventListener('submit', event => {
    const action = event.submitter?.value;
    if (action === 'submit' && dirty.size) {
      window.alert('Save or commit your changed days before submitting the week.');
      event.preventDefault(); return;
    }
    if ([...dirty].some(id => id !== form.id) && !window.confirm('Other days have unsaved changes. Continuing will discard those changes. Continue?')) {
      event.preventDefault(); return;
    }
    const prompt = action === 'commit_day' ? 'Commit this day? You cannot change it afterwards. Only an administrator can correct or unlock it.' : action === 'submit' ? 'Submit this week to admin? This locks the weekly record.' : null;
    if (prompt && !window.confirm(prompt)) { event.preventDefault(); return; }
    dirty.clear();
  }));
  window.addEventListener('beforeunload', event => {
    if (dirty.size) { event.preventDefault(); event.returnValue = ''; }
  });
}

// Label table cells for the mobile card layout while preserving desktop tables.
document.querySelectorAll('.table-wrap table').forEach(table => {
  const headings = [...table.querySelectorAll('thead th')].map(cell => cell.textContent.trim());
  table.querySelectorAll('tbody tr').forEach(row => [...row.cells].forEach((cell,index) => { cell.dataset.label = headings[index] || ''; }));
});
// Announce new notices without refreshing forms or marking notices as read.
const liveNotices = document.querySelector('[data-notice-status]');
if (liveNotices) {
  const checkNotices = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(liveNotices.dataset.noticeStatus, {headers: {'Accept':'application/json'}, cache:'no-store'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const status = await response.json();
      if (status.latest > Number(liveNotices.dataset.noticeLatest)) liveNotices.classList.remove('hidden');
    } catch (_) { /* Keep the current page usable while offline. */ }
  };
  setInterval(checkNotices, 30000);
  document.addEventListener('visibilitychange',checkNotices);
}

// Keep visible dates in DD.MM.YYYY while retaining the device calendar picker.
document.querySelectorAll('input.formatted-date').forEach(field => {
  const wrapper = document.createElement('span'); wrapper.className = 'date-entry';
  field.parentNode.insertBefore(wrapper, field); wrapper.appendChild(field);
  const calendar = document.createElement('span'); calendar.className = 'date-calendar';
  const icon = document.createElement('span'); icon.textContent = '▦'; icon.setAttribute('aria-hidden','true');
  const picker = document.createElement('input'); picker.type = 'date'; picker.className = 'native-date-picker';
  picker.setAttribute('aria-label', `Choose ${field.name.replaceAll('_',' ')}`);
  if (field.dataset.min) picker.min = field.dataset.min;
  calendar.append(icon,picker); wrapper.appendChild(calendar);
  const validate = () => {
    const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(field.value);
    if (!field.value) { field.setCustomValidity(''); picker.value = ''; return; }
    const iso = match ? `${match[3]}-${match[2]}-${match[1]}` : '';
    const value = iso ? new Date(`${iso}T12:00:00Z`) : null;
    const valid = value && !Number.isNaN(value.getTime()) && value.toISOString().slice(0,10) === iso;
    field.setCustomValidity(!valid ? 'Enter a valid date as DD.MM.YYYY.' : field.dataset.min && iso < field.dataset.min ? 'Choose today or a future date.' : '');
    picker.value = valid ? iso : '';
  };
  field.addEventListener('input', () => {
    if (/^\d{4}-\d{2}-\d{2}$/.test(field.value)) field.value = field.value.split('-').reverse().join('.');
    else { const digits = field.value.replace(/\D/g,'').slice(0,8); field.value = [digits.slice(0,2),digits.slice(2,4),digits.slice(4)].filter(Boolean).join('.'); }
    validate();
  });
  picker.addEventListener('click', () => { if (picker.showPicker) { try { picker.showPicker(); } catch (_) {} } });
  picker.addEventListener('change', () => { field.value = picker.value ? picker.value.split('-').reverse().join('.') : ''; validate(); });
  validate();
});

const paymentForm = document.querySelector('.payment-form');
if (paymentForm) {
  const cash = paymentForm.elements.cash_amount;
  const transfer = paymentForm.elements.transfer_amount;
  const due = Number(paymentForm.dataset.amountDue);
  const cents = value => {
    if (!/^\d+(\.\d{1,2})?$/.test(value)) return null;
    const [whole, fraction=''] = value.split('.');
    return Number(whole)*100 + Number(fraction.padEnd(2,'0'));
  };
  const updatePayment = () => {
    const first = cents(cash.value), second = cents(transfer.value);
    const remaining = first === null || second === null ? null : due-first-second;
    const display = paymentForm.querySelector('.payment-check');
    const money = value => (value/100).toLocaleString('en-AU',{style:'currency',currency:'AUD'});
    display.textContent = remaining === null ? 'Enter valid cash and transfer amounts, with up to two decimal places.' : remaining === 0 ? `Ready to process: ${money(first)} cash + ${money(second)} transfer.` : remaining > 0 ? `${money(remaining)} still to allocate.` : `Amounts exceed the amount due by ${money(-remaining)}.`;
    paymentForm.querySelector('button[type=submit]').disabled = remaining !== 0;
  };
  cash.addEventListener('input',updatePayment); transfer.addEventListener('input',updatePayment); updatePayment();
  paymentForm.addEventListener('submit', () => { paymentForm.querySelector('button[type=submit]').disabled = true; });
}
document.querySelectorAll('.print-slip').forEach(button => button.addEventListener('click',() => window.print()));

const prestartForm = document.querySelector('.prestart-form');
if (prestartForm) {
  prestartForm.querySelectorAll('.prestart-check').forEach(row => {
    const note = row.querySelector('textarea'), details = row.querySelector('details');
    const update = () => {
      const failed = ['fail','no'].includes(row.querySelector('[data-prestart-check]:checked')?.value);
      note.required = failed; row.classList.toggle('has-failure',failed);
      if (failed) details.open = true;
    };
    row.querySelectorAll('[data-prestart-check]').forEach(input => input.addEventListener('change',update));
    note.addEventListener('invalid', () => { details.open = true; });
    update();
    const file = row.querySelector('input[type=file]'), status = row.querySelector('.photo-status');
    const updatePhoto = () => {
      file.setCustomValidity(file.files[0]?.size > 5*1024*1024 ? 'Choose a photo smaller than 5 MB.' : '');
      status.hidden = !file.files.length;
      status.querySelector('span').textContent = file.files[0]?.name || '';
    };
    file.addEventListener('change',updatePhoto);
    row.querySelector('.photo-remove').addEventListener('click', () => { file.value = ''; updatePhoto(); });
  });
  const updateFit = () => { prestartForm.elements.notes.required = prestartForm.querySelector('[name=fit_for_duty]:checked')?.value === 'no'; };
  prestartForm.querySelectorAll('[name=fit_for_duty]').forEach(input => input.addEventListener('change',updateFit)); updateFit();
  const canvas = document.querySelector('#prestart-signature'), ctx = canvas.getContext('2d');
  const hidden = prestartForm.elements.signature_strokes, error = prestartForm.querySelector('.signature-error');
  let strokes = [], current = null;
  try { const saved = JSON.parse(hidden.value); if (Array.isArray(saved) && saved.length <= 100 && saved.every(s => Array.isArray(s) && s.every(p => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite)))) strokes = saved; } catch (_) {}
  const draw = () => {
    ctx.clearRect(0,0,600,200); ctx.strokeStyle='#111827';ctx.lineWidth=3;ctx.lineCap='round';ctx.lineJoin='round';
    for (const stroke of strokes) {
      if (!stroke.length) continue;
      ctx.beginPath();ctx.moveTo(...stroke[0]);for (const point of stroke.slice(1)) ctx.lineTo(...point);ctx.stroke();
    }
    hidden.value = JSON.stringify(strokes.filter(s => s.length >= 2));
  };
  const point = event => {
    const rect=canvas.getBoundingClientRect();
    return [Math.round(Math.max(0,Math.min(600,(event.clientX-rect.left)*600/rect.width))*10)/10,
            Math.round(Math.max(0,Math.min(200,(event.clientY-rect.top)*200/rect.height))*10)/10];
  };
  canvas.addEventListener('pointerdown', event => {
    if (!event.isPrimary || (event.pointerType==='mouse' && event.button!==0)) return;
    event.preventDefault(); canvas.setPointerCapture(event.pointerId);
    current=[point(event)];strokes.push(current);error.hidden=true;draw();
  });
  canvas.addEventListener('pointermove', event => {
    if (!current || !event.isPrimary) return;
    event.preventDefault();const p=point(event),last=current[current.length-1];
    if (Math.hypot(p[0]-last[0],p[1]-last[1])>=1) {current.push(p);draw();}
  });
  const finish = () => {current=null;strokes=strokes.filter(s => s.length>=2);draw();};
  canvas.addEventListener('pointerup',finish);canvas.addEventListener('pointercancel',finish);
  prestartForm.querySelector('.signature-clear').addEventListener('click', () => {strokes=[];current=null;draw();canvas.focus();});
  draw();
  prestartForm.addEventListener('submit', event => {
    const pts=strokes.flat(),length=strokes.reduce((total,s) => total+s.slice(1).reduce((n,p,i) => n+Math.hypot(p[0]-s[i][0],p[1]-s[i][1]),0),0);
    if (!pts.length || length<40 || Math.max(...pts.map(p=>p[0]))-Math.min(...pts.map(p=>p[0]))<10 || Math.max(...pts.map(p=>p[1]))-Math.min(...pts.map(p=>p[1]))<5) {
      event.preventDefault();error.hidden=false;canvas.focus();canvas.scrollIntoView({block:'center'});return;
    }
    const size=[...prestartForm.querySelectorAll('input[type=file]')].reduce((n,f)=>n+(f.files[0]?.size||0),0);
    if(size>30*1024*1024) {event.preventDefault();window.alert('Keep the total photo size below 30 MB.');return;}
    prestartForm.querySelector('button[type="submit"]').disabled = true;
  });
}
