(() => {
  const button = document.querySelector('.bf-office-toggle');
  if (!button) return;
  const nav = document.getElementById(button.getAttribute('aria-controls'));
  const desktop = window.matchMedia('(min-width: 980px)');
  function expand(open) {
    nav.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  }
  function resize() {
    button.hidden = desktop.matches;
    expand(desktop.matches);
  }
  button.addEventListener('click', () => expand(nav.hidden));
  nav.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !desktop.matches) {
      expand(false);
      button.focus();
    }
  });
  desktop.addEventListener('change', resize);
  resize();

  const search = document.getElementById('bf-menu-search');
  if (!search) return;
  const status = document.getElementById('bf-menu-search-status');
  const links = Array.from(nav.querySelectorAll('a[data-bf-search]'));
  const groups = Array.from(nav.querySelectorAll('.bf-menu-group'));
  const normalise = text => text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  let matches = links;
  search.closest('.bf-menu-search').hidden = false;
  function filter() {
    const words = normalise(search.value).split(' ').filter(Boolean);
    matches = links.filter(link => words.every(word => normalise(link.dataset.bfSearch).includes(word)));
    links.forEach(link => { link.hidden = !matches.includes(link); });
    groups.forEach(group => {
      group.hidden = !group.querySelector('a[data-bf-search]:not([hidden])');
      // Keep the Setup heading legible when one of its child links matches.
      const heading = group.querySelector('h2 a[data-bf-search]');
      if (heading && !group.hidden) heading.hidden = false;
    });
    status.textContent = words.length ? (matches.length ? `${matches.length} matching pages` : 'No matching page. Try freight, invoices or POD.') : '';
    if (words.length) expand(true);
  }
  search.addEventListener('input', filter);
  search.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      search.value = '';
      filter();
    } else if ((event.key === 'ArrowDown' || event.key === 'Enter') && matches.length) {
      event.preventDefault();
      if (event.key === 'Enter' && matches.length === 1) matches[0].click();
      else matches[0].focus();
    }
  });
})();
