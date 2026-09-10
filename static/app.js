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
const sheet = document.querySelector('#timesheet-form');
if (sheet && sheet.dataset.locked !== 'true') {
  const fields = [...sheet.querySelectorAll('.hours-input')];
  const update = () => {
    const units = fields.reduce((total, field) => total + Math.round((Number(field.value) || 0) * 100), 0);
    const cents = Math.round(units * Number(sheet.dataset.rate) / 100);
    document.querySelector('#total-hours').textContent = (units / 100).toLocaleString('en-AU', {maximumFractionDigits: 2});
    document.querySelector('#total-pay').textContent = (cents / 100).toLocaleString('en-AU', {style: 'currency', currency: 'AUD'});
  };
  fields.forEach(field => field.addEventListener('input', update));
  update();
  let dirty = false;
  sheet.addEventListener('input', () => { dirty = true; });
  sheet.addEventListener('submit', event => {
    if (event.submitter?.value === 'submit' && !window.confirm('Submit this week to admin? Check your hours and activities first. This will lock the weekly record.')) {
      event.preventDefault();
      return;
    }
    dirty = false;
  });
  window.addEventListener('beforeunload', event => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
}
