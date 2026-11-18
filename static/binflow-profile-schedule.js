'use strict';
document.querySelectorAll('[data-site-schedule]').forEach(editor => {
  const frequency = editor.querySelector('[data-frequency]');
  const single = editor.querySelector('[data-single-day]');
  const multiple = editor.querySelector('[data-multiple-days]');
  const boxes = [...multiple.querySelectorAll('input')];
  const hint = editor.querySelector('[data-schedule-hint]');
  const singleDay = ['once','fortnightly','four_weekly','eight_weekly','last_monthly'];
  const multiDay = ['twice','three','custom'];
  function refresh() {
    const kind = frequency.value;
    single.hidden = !singleDay.includes(kind);
    single.querySelector('select').disabled = single.hidden;
    multiple.hidden = !multiDay.includes(kind);
    boxes.forEach(box => { box.disabled = multiple.hidden; });
    const selected = boxes.filter(box => box.checked).map(box => box.parentElement.textContent.trim().slice(0,3));
    multiple.querySelector('[data-days-caption]').textContent = selected.join(', ') || 'Choose days';
    hint.textContent = ({
      three:'Choose three collection days.', twice:'Choose two collection days.',
      daily:'Monday through Sunday, including weekends.', weekdays:'Monday through Friday.',
      custom:'Choose every day this site receives a collection each week.',
      last_monthly:'Choose the day for the last occurrence each month.', adhoc:'Book a pickup when the site requests one.',
      review:'Review the current agreement before replacing it.', paused:'Recurring collections are paused.'
    })[kind] || '';
  }
  frequency.addEventListener('change', () => {
    if (frequency.value === 'three') boxes.forEach(box => { box.checked = ['0','2','4'].includes(box.value); });
    refresh();
  });
  boxes.forEach(box => box.addEventListener('change', refresh));
  refresh();
});
