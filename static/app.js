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
  prestartForm.querySelectorAll('[data-prestart-check]').forEach(select => {
    const note = select.closest('.prestart-check').querySelector('textarea');
    const update = () => { note.required = ['fail','no'].includes(select.value); };
    select.addEventListener('change', update); update();
  });
  const fit = prestartForm.elements.fit_for_duty;
  const updateFit = () => { prestartForm.elements.notes.required = fit.value === 'no'; };
  fit.addEventListener('change',updateFit); updateFit();
  prestartForm.addEventListener('submit', () => { prestartForm.querySelector('button[type="submit"]').disabled = true; });
}
