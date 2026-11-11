(() => {
  const form = document.querySelector('[data-it-booking]');
  if (!form) return;
  const {origin, destination, pickup_date: pickup, delivery_date: delivery, handling, job_type: job, goods, vehicle, hours} = form.elements;
  const rates = JSON.parse(form.dataset.rates);
  const lanes = JSON.parse(form.dataset.lanes);
  const rigid = JSON.parse(form.dataset.rigid);
  const money = new Intl.NumberFormat('en-AU', {style:'currency',currency:'AUD'});
  const dollars = cents => money.format(cents / 100);
  const price = form.querySelector('[data-rate]');
  const hint = form.querySelector('[data-transit-hint]');
  const hoursPanel = form.querySelector('[data-rigid-hours]');
  let anchor = pickup.value || !delivery.value ? pickup : delivery;
  let suggested = null;

  function isLocal() {
    const oldRoute = rigid.legacyRoute;
    return vehicle.value === 'RIGID' && !(oldRoute && oldRoute[0] === origin.value && oldRoute[1] === destination.value);
  }
  function roundedProduct(amount, units, divisor) {
    return Number((BigInt(amount) * BigInt(units) + BigInt(divisor / 2)) / BigInt(divisor));
  }
  function updatePrice() {
    if (!price) return;
    let quote = rates[[origin.value, destination.value, vehicle.value].join(':')];
    let billing = '';
    if (isLocal()) {
      quote = null;
      if (hours.value && hours.validity.valid) {
        const units = Math.max(rigid.minimum * 100, Math.round(hours.valueAsNumber * 100));
        billing = (units / 100) + ' billable hours · ';
        if (rigid.hourly != null) {
          const base = roundedProduct(rigid.hourly, units, 100);
          const fuel = roundedProduct(base, rigid.bps, 10000);
          quote = {base, bps:rigid.bps, fuel, total:base + fuel};
        }
      }
    }
    price.textContent = billing + (quote
      ? 'Base freight ' + dollars(quote.base) + ' · Fuel surcharge (' + quote.bps / 100 + '%) ' + dollars(quote.fuel) + ' · Total ' + dollars(quote.total) + ' ex GST'
      : 'Price to be confirmed');
    form.elements.shown_rate.value = quote ? String(quote.base) + (quote.bps ? '+' + quote.bps : '') : '';
    form.elements.shown_rate.disabled = false;
  }
  function estimate(routeChanged = false) {
    const days = isLocal() ? 0 : lanes[[origin.value, destination.value].sort().join('-')];
    const other = anchor === pickup ? delivery : pickup;
    if (days == null) {
      if (routeChanged && suggested) suggested.value = '';
      suggested = null;
      hint.textContent = origin.value && destination.value ? 'Enter both dates for this route.' : 'Select a route to see its transit time.';
      return;
    }
    hint.textContent = isLocal() ? 'Local delivery defaults to the pickup date.' : days + ' calendar days in either direction. You can adjust the estimated date.';
    if (anchor.valueAsDate && (routeChanged || !other.value || suggested === other)) {
      const date = anchor.valueAsDate;
      date.setUTCDate(date.getUTCDate() + (anchor === pickup ? days : -days));
      other.value = date.toISOString().slice(0, 10);
      suggested = other;
    }
  }
  function updateHandling() {
    const bulk = !handling || handling.value === 'bulk';
    goods.required = bulk;
    form.querySelector('[data-goods-label]').textContent = bulk ? 'Goods and quantities (one line per item)' : 'Load notes (optional)';
  }
  function setVehicle() {
    const local = isLocal();
    hoursPanel.hidden = !local;
    hours.disabled = !local;
    hours.required = local;
    for (const option of destination.options) option.disabled = local && option.value !== origin.value;
    if (local) destination.value = origin.value;
    if (handling) {
      handling.querySelector('option[value="scanned"]').disabled = local;
      if (local) handling.value = 'bulk';
    }
    updateHandling();
    updatePrice();
  }
  vehicle.addEventListener('change', () => {
    if (!isLocal() && origin.value === destination.value) destination.value = '';
    setVehicle();
    estimate(true);
  });
  origin.addEventListener('change', () => { setVehicle(); estimate(true); });
  destination.addEventListener('change', () => { updatePrice(); estimate(true); });
  hours.addEventListener('input', updatePrice);
  [pickup, delivery].forEach(el => el.addEventListener('change', () => {
    if (el !== suggested) {
      anchor = el;
    } else {
      suggested = null;
      if (!(el === pickup ? delivery : pickup).value) anchor = el;
    }
    estimate();
  }));
  job?.addEventListener('change', () => {
    if (handling) handling.value = isLocal() || job.value === 'external' ? 'bulk' : 'scanned';
    updateHandling();
  });
  handling?.addEventListener('change', updateHandling);
  setVehicle();
  estimate();
})();
