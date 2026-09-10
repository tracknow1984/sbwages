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
