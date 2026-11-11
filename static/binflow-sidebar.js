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
})();
