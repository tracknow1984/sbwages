(() => {
  const form = document.querySelector('[data-it-booking]');
  if (!form) return;
  const {origin, destination, pickup_date: pickup, delivery_date: delivery, handling, job_type: job, goods} = form.elements;
  const lanes = JSON.parse(form.dataset.lanes);
  const hint = form.querySelector('[data-transit-hint]');
  let anchor = pickup.value || !delivery.value ? pickup : delivery;
  let suggested = null;
  function estimate(routeChanged = false) {
    const days = lanes[[origin.value, destination.value].sort().join('-')];
    const other = anchor === pickup ? delivery : pickup;
    if (!days) {
      if (routeChanged && suggested) suggested.value = '';
      suggested = null;
      hint.textContent = origin.value && destination.value ? 'Enter both dates for this route.' : 'Select a route to see its transit time.';
      return;
    }
    hint.textContent = days + ' calendar days in either direction. You can adjust the estimated date.';
    if (anchor.valueAsDate && (routeChanged || !other.value || suggested === other)) {
      const date = anchor.valueAsDate;
      date.setUTCDate(date.getUTCDate() + (anchor === pickup ? days : -days));
      other.value = date.toISOString().slice(0, 10);
      suggested = other;
    }
  }
  [origin, destination].forEach(el => el.addEventListener('change', () => estimate(true)));
  [pickup, delivery].forEach(el => el.addEventListener('change', () => {
    if (el !== suggested) {
      anchor = el;
    } else {
      suggested = null;
      if (!(el === pickup ? delivery : pickup).value) anchor = el;
    }
    estimate();
  }));
  function updateHandling() {
    const bulk = handling.value === 'bulk';
    goods.required = bulk;
    form.querySelector('[data-goods-label]').textContent = bulk ? 'Goods and quantities (one line per item)' : 'Load notes (optional)';
  }
  job.addEventListener('change', () => {
    handling.value = job.value === 'external' ? 'bulk' : 'scanned';
    updateHandling();
  });
  handling.addEventListener('change', updateHandling);
  estimate();
  updateHandling();
})();
