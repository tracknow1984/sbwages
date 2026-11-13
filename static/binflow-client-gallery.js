(() => {
  const gallery = document.querySelector('.gallery');
  const scenes = Array.from(gallery.querySelectorAll('.scene'));
  const toggle = gallery.querySelector('.gallery-toggle');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let playing = !motion.matches;
  let index = 0;
  let timer;

  function schedule() {
    clearTimeout(timer);
    toggle.textContent = playing ? 'Pause slideshow' : 'Play slideshow';
    if (!playing || document.hidden) return;
    timer = setTimeout(() => {
      scenes[index].hidden = true;
      index = (index + 1) % scenes.length;
      scenes[index].hidden = false;
      schedule();
    }, 6000);
  }

  toggle.hidden = false;
  toggle.addEventListener('click', () => { playing = !playing; schedule(); });
  motion.addEventListener('change', () => { playing = !motion.matches; schedule(); });
  document.addEventListener('visibilitychange', schedule);
  schedule();
})();
