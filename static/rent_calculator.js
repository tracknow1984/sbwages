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
    const incomeCents = sqm * Math.round(price * 100);
    const subsidy = Math.max(0, 100000000 - incomeCents) / 100;
    document.getElementById('rent-vacant').textContent = `${number.format(40000 - sqm)} sqm`;
    document.getElementById('rent-break-even').textContent = `${money.format(1000000 / sqm)} / sqm`;
    document.getElementById('rent-surplus').textContent = money.format(Math.max(0, incomeCents - 100000000) / 100);
    document.getElementById('rent-actual').textContent = money.format(1000000);
    document.getElementById('rent-subsidy').textContent = money.format(subsidy);
    document.getElementById('rent-subsidy-monthly').textContent = money.format(subsidy / 12);
    document.getElementById('rent-save-area').value = String(sqm);
    document.getElementById('rent-save-rate').value = price.toFixed(2);
    document.getElementById('rent-formula').textContent = `${number.format(sqm)} sqm × ${money.format(price)} per sqm / year`;
    document.getElementById('rent-annual').textContent = money.format(annual);
    document.getElementById('rent-monthly').textContent = money.format(annual / 12);
    document.getElementById('rent-weekly').textContent = money.format(annual / 52);
    area.setAttribute('aria-valuetext', `${number.format(sqm)} square metres`);
    renderPartners();
    rate.setAttribute('aria-valuetext', `${money.format(price)} per square metre per year`);
  }
  function renderPartners() {
    const fields = Array.from(document.querySelectorAll('.partner-share'));
    if (fields.length !== 4) return;
    const shares = fields.map(el => Math.round(Number(el.value) * 100));
    const valid = fields.every(el => el.value !== '' && el.validity.valid) && shares.reduce((a,b) => a+b, 0) === 10000;
    document.getElementById('partner-validation').textContent = `Total ownership: ${(shares.reduce((a,b) => a+b, 0) / 100).toFixed(2)}%. ${valid ? 'Ready to calculate.' : 'Enter shares totalling exactly 100% to calculate.'}`;
    document.getElementById('save-partners').disabled = !valid;
    const body = document.getElementById('partner-results');
    body.replaceChildren();
    if (!valid) return;
    // Largest-remainder allocation keeps each column equal to the lot total in cents.
    const split = cents => {
      const raw = shares.map(bps => cents * bps);
      const amounts = raw.map(n => Math.floor(n / 10000));
      const order = raw.map((n,i) => ({i, remainder:n % 10000})).sort((a,b) => b.remainder-a.remainder || a.i-b.i);
      const left = cents - amounts.reduce((a,b) => a+b, 0);
      for(let i=0;i<left;i++) amounts[order[i].i]++;
      return amounts;
    };
    const yearly = Array.from(document.querySelectorAll('#partner-years [data-subsidy]')).map(el => split(Number(el.dataset.subsidy)));
    const preview = split(Math.max(0, 100000000 - Number(area.value)*Math.round(Number(rate.value)*100)));
    const bill = split(100000000);
    const totals = shares.map((_,i) => yearly.reduce((sum,row) => sum+row[i],0));
    const addRow = values => { const tr=document.createElement('tr'); const labels=['Partner','Ownership','Owned sqm','Annual bill share','Current slider subsidy / year','Year 1 subsidy','Year 2 subsidy','Year 3 subsidy','Year 4 subsidy','Total saved subsidy'];values.forEach((value,i)=>{const td=document.createElement('td');td.dataset.label=labels[i];td.textContent=value;tr.appendChild(td)});body.appendChild(tr); };
    shares.forEach((bps,i) => addRow([document.getElementById(`partner-name-${i+1}`).value,`${(bps/100).toFixed(2)}%`,number.format(40000*bps/10000),money.format(bill[i]/100),money.format(preview[i]/100),...[0,1,2,3].map(y=>yearly[y] ? money.format(yearly[y][i]/100) : 'Not saved'),money.format(totals[i]/100)]));
    addRow(['TOTAL','100%','40,000',money.format(1000000),money.format(preview.reduce((a,b)=>a+b,0)/100),...[0,1,2,3].map(y=>yearly[y] ? money.format(yearly[y].reduce((a,b)=>a+b,0)/100) : 'Not saved'),money.format(totals.reduce((a,b)=>a+b,0)/100)]);
  }
  document.querySelectorAll('#partner-form input:not([type=hidden])').forEach(el => el.addEventListener('input', () => {
    document.getElementById('partner-save-status').textContent = 'Unsaved ownership changes — select Save ownership to keep them.';
    renderPartners();
  }));
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
