(() => {
  const screen = document.querySelector('[data-bale-label]');
  if (!screen) return;
  const thermal = screen.querySelector('[data-bale-thermal]');
  const a4 = screen.querySelector('[data-bale-a4]');
  const key = 'binflow.baleLabelFormat';

  function select(format) {
    const isThermal = format === 'thermal';
    screen.dataset.format = format;
    thermal.classList.toggle('primary', isThermal);
    thermal.classList.toggle('secondary', !isThermal);
    a4.classList.toggle('primary', !isThermal);
    a4.classList.toggle('secondary', isThermal);
  }

  function remember(format) {
    select(format);
    try { localStorage.setItem(key, format); } catch { /* Printing also works without storage. */ }
  }

  try { select(localStorage.getItem(key) === 'a4' ? 'a4' : 'thermal'); } catch { select('thermal'); }
  thermal.addEventListener('click', () => remember('thermal'));
  a4.addEventListener('click', () => {
    remember('a4');
    window.print();
  });
})();
