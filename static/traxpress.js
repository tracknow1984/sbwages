'use strict';
(() => {
  const stock = document.querySelector('#tx-stock-form');
  if (stock) {
    const container = document.querySelector('#tx-stock-lines');
    const add = document.querySelector('#tx-add-line');
    const update = () => {
      const rows = [...container.querySelectorAll('.tx-stock-row')];
      const total = rows.reduce((sum, row) => sum + Math.max(0, Number(row.querySelector('[name="quantity"]').value) || 0), 0);
      document.querySelector('#tx-count').textContent = total.toLocaleString('en-AU') + ' tyres';
      rows.forEach(row => { row.querySelector('.tx-remove').disabled = rows.length === 1; });
      add.disabled = rows.length >= 200;
    };
    add.addEventListener('click', () => {
      if (container.children.length >= 200) return;
      const row = container.firstElementChild.cloneNode(true);
      row.querySelectorAll('input').forEach(input => { input.value = ''; });
      container.append(row); update(); row.querySelector('input').focus();
    });
    container.addEventListener('click', event => {
      if (event.target.closest('.tx-remove') && container.children.length > 1) {
        event.target.closest('.tx-stock-row').remove(); update();
      }
    });
    container.addEventListener('input', update); update();
  }
  const handling = document.querySelector('#tx-handling-form');
  if (handling) {
    const field = name => handling.elements.namedItem(name);
    const money = cents => (cents / 100).toLocaleString('en-AU', {style:'currency',currency:'AUD'});
    const update = () => {
      if (!handling.dataset.unload) return;
      const val = name => Math.max(0, Number(field(name)?.value) || 0);
      const rate = name => Number(handling.dataset[name]);
      const hoursUnits = Math.round(val('hours') * 100);
      const bulk = handling.dataset.kind === 'bulk_unload';
      const hoursCents = Math.floor((hoursUnits * rate(bulk ? 'unload' : 'prepare') + 50) / 100);
      const lines = [[bulk ? 'Unloading incl. forklift' : 'Preparation & loading time', hoursCents]];
      if (!bulk) {
        lines.push(['Pallet loading', val('pallets_loaded') * rate('loading')],
          ['Pallets supplied', val('pallets_supplied') * rate('pallet')], ['Strapping', val('straps') * rate('strap')]);
        field('pallets_supplied').setCustomValidity(val('pallets_supplied') > val('pallets_loaded') ? 'Supplied pallets cannot exceed loaded pallets.' : '');
      }
      document.querySelector('#tx-charge').textContent = money(lines.reduce((total, line) => total + line[1], 0));
      document.querySelector('#tx-breakdown').textContent = lines.map(([label, cents]) => `${label}: ${money(cents)}`).join('\n');
    };
    handling.addEventListener('input', update); update();
    const photo = document.querySelector('#delivery-note');
    if (photo) {
      const camera = document.querySelector('#tx-camera-input');
      const status = document.querySelector('#tx-photo-name');
      const check = () => {
        const file = photo.files[0];
        photo.setCustomValidity(file && file.size > 5 * 1024 * 1024 ? 'Choose a photo up to 5 MB.' : '');
        status.textContent = file ? file.name : 'No photo selected';
      };
      document.querySelector('#tx-camera').addEventListener('click', () => camera.click());
      camera.addEventListener('change', () => { if (camera.files.length) { photo.files = camera.files; check(); } });
      photo.addEventListener('change', check);
    }
  }
})();
