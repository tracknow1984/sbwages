(() => {
  const select = document.querySelector('[data-xero-contacts]');
  const input = document.querySelector('[data-xero-search]');
  if (!select || !input) return;
  const count = document.querySelector('[data-xero-count]');
  const initialCount = count.textContent;
  const options = [...select.options];
  const normalise = text => text.toLocaleLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
  const keys = new Map(options.filter(o => o.hasAttribute('data-contact-search')).map(o =>
    [o, normalise(o.text+' '+o.dataset.contactSearch)]));
  function filter() {
    const query = normalise(input.value.trim());
    const selected = select.value;
    const visible = options.filter(o => !keys.has(o) || keys.get(o).includes(query));
    select.replaceChildren(...visible);
    select.value = visible.some(o => o.value === selected) ? selected : '';
    const matches = visible.filter(o => keys.has(o)).length;
    count.textContent = query ? matches+' of '+keys.size+' contacts match' : initialCount;
  }
  input.addEventListener('input', filter);
  if (input.value) filter();
})();
