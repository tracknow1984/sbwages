(() => {
  'use strict';
  if (!document.getElementById('rent-calculator')) return;
  const money = new Intl.NumberFormat('en-AU', {style: 'currency', currency: 'AUD'});
  const number = new Intl.NumberFormat('en-AU');
  const area = document.getElementById('rent-area');
  const rate = document.getElementById('rent-rate');
  const saved = document.getElementById('rent-calculator').dataset;
  area.value = saved.area;
  rate.value = saved.rate;
  document.getElementById('rent-area-number').value = area.value;
  document.getElementById('rent-rate-number').value = Number(rate.value).toFixed(2);
  function render() {
    const sqm = Number(area.value);
    const price = Number(rate.value);
    const annual = sqm * price;
    const subsidy = sqm * Math.max(0, 2500 - Math.round(price * 100)) / 100;
    document.getElementById('rent-actual').textContent = money.format(sqm * 25);
    document.getElementById('rent-subsidy').textContent = money.format(subsidy);
    document.getElementById('rent-subsidy-monthly').textContent = money.format(subsidy / 12);
    document.getElementById('rent-save-area').value = String(sqm);
    document.getElementById('rent-save-rate').value = price.toFixed(2);
    document.getElementById('rent-formula').textContent = `${number.format(sqm)} sqm × ${money.format(price)} per sqm / year`;
    document.getElementById('rent-annual').textContent = money.format(annual);
    document.getElementById('rent-monthly').textContent = money.format(annual / 12);
    document.getElementById('rent-weekly').textContent = money.format(annual / 52);
    area.setAttribute('aria-valuetext', `${number.format(sqm)} square metres`);
    rate.setAttribute('aria-valuetext', `${money.format(price)} per square metre per year`);
  }
  function bind(slider, input, decimals) {
    slider.addEventListener('input', () => {
      input.value = Number(slider.value).toFixed(decimals);
      render();
    });
    input.addEventListener('input', () => {
      if (input.value === '' || !input.validity.valid) return;
      slider.value = input.value;
      render();
    });
    input.addEventListener('change', () => {
      const value = input.value === '' ? Number(slider.value) : Number(input.value);
      const bounded = Math.min(Number(slider.max), Math.max(Number(slider.min), Number.isFinite(value) ? value : Number(slider.value)));
      slider.value = bounded.toFixed(decimals);
      input.value = Number(slider.value).toFixed(decimals);
      render();
    });
  }
  bind(area, document.getElementById('rent-area-number'), 0);
  bind(rate, document.getElementById('rent-rate-number'), 2);
  render();
})();
