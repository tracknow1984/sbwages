(() => {
  const nav = document.querySelector('.empire-nav');
  if (!nav) return;
  const button = nav.querySelector('.empire-nav__more');
  const tools = document.getElementById(button.getAttribute('aria-controls'));
  const compact = window.matchMedia('(max-width: 63.99rem)');
  const activeTool = tools.querySelector('[aria-current="page"]');

  function setOpen(open) {
    tools.hidden = compact.matches && !open;
    button.setAttribute('aria-expanded', String(open));
  }
  function resize() {
    const focusMovesToButton = compact.matches && tools.contains(document.activeElement);
    const focusMovesToLinks = !compact.matches && document.activeElement === button;
    nav.classList.toggle('is-compact', compact.matches);
    button.hidden = !compact.matches;
    setOpen(false);
    if (focusMovesToButton) button.focus();
    if (focusMovesToLinks) (activeTool || tools.querySelector('a')).focus();
  }
  if (activeTool) {
    button.classList.add('is-current');
    button.setAttribute('aria-label', 'More sections, current page: ' + activeTool.textContent.trim());
  }
  button.addEventListener('click', () => setOpen(tools.hidden));
  nav.addEventListener('keydown', event => {
    if (event.key === 'Escape' && compact.matches && !tools.hidden) {
      setOpen(false);
      button.focus();
    }
  });
  document.addEventListener('click', event => {
    if (!nav.contains(event.target) && compact.matches && !tools.hidden) setOpen(false);
  });
  nav.addEventListener('focusout', event => {
    if (event.relatedTarget && !nav.contains(event.relatedTarget) && compact.matches) setOpen(false);
  });
  compact.addEventListener('change', resize);
  resize();
})();
