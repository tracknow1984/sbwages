(() => {
  const preview = document.getElementById('it-transfer-preview');
  if (!preview) return;
  const body = preview.querySelector('[data-preview-body]');
  const closeButton = preview.querySelector('button');
  const startingHues = [210, 24, 145, 281, 48, 344, 180, 83, 242, 320, 8, 164];
  function colour(element, slot) {
    const hue = startingHues[slot] ?? ((slot - startingHues.length) * 137.508 + 99) % 360;
    element.style.setProperty('--it-accent', 'hsl(' + hue + ' 61% 38%)');
    element.style.setProperty('--it-tint', 'hsl(' + hue + ' 60% 94%)');
  }
  document.querySelectorAll('[data-client-colour]').forEach(node => colour(node, Number(node.dataset.clientColour)));

  let trigger = null, pinned = false, closeTimer, returnFocus = false;
  function position() {
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect(), gap = 6, margin = 10;
    const width = preview.offsetWidth, height = preview.offsetHeight;
    const right = rect.right + gap, left = rect.left - gap - width;
    let x = rect.left, y = rect.bottom + gap;
    if (right + width <= innerWidth - margin) { x = right; y = rect.top; }
    else if (left >= margin) { x = left; y = rect.top; }
    else if (y + height > innerHeight - margin && rect.top - gap >= height) y = rect.top - gap - height;
    preview.style.left = Math.max(margin, Math.min(x, innerWidth - width - margin)) + 'px';
    preview.style.top = Math.max(margin, Math.min(y, innerHeight - height - margin)) + 'px';
  }
  function hide(restore = false) {
    clearTimeout(closeTimer);
    if (!trigger) return;
    const prior = trigger;
    prior.setAttribute('aria-expanded','false');
    preview.hidden = true;
    trigger = null;
    pinned = false;
    if (restore) {
      returnFocus = true;
      prior.focus({preventScroll:true});
      returnFocus = false;
    }
  }
  function show(link) {
    clearTimeout(closeTimer);
    if (trigger === link) return true;
    const template = document.getElementById('it-booking-' + link.dataset.transferPreview);
    if (!template) return false;
    hide();
    trigger = link;
    body.replaceChildren(template.content.cloneNode(true));
    colour(preview, Number(link.dataset.clientColour));
    link.setAttribute('aria-expanded','true');
    preview.hidden = false;
    preview.scrollTop = 0;
    position();
    return true;
  }
  function leave() {
    clearTimeout(closeTimer);
    if (!pinned && document.activeElement !== trigger) closeTimer = setTimeout(() => hide(), 250);
  }
  for (const link of document.querySelectorAll('[data-transfer-preview]')) {
    let pointerType;
    link.addEventListener('pointerdown', event => { pointerType = event.pointerType; });
    link.addEventListener('pointerenter', event => {
      if (event.pointerType !== 'touch' && !pinned) show(link);
    });
    link.addEventListener('pointerleave', event => {
      if (event.pointerType !== 'touch' && trigger === link) leave();
    });
    link.addEventListener('focus', () => { if (!returnFocus) show(link); });
    link.addEventListener('blur', event => {
      if (trigger === link && !preview.contains(event.relatedTarget)) hide();
    });
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (event.detail && (event.pointerType || pointerType) === 'mouse') return;
      if (!show(link)) return;
      event.preventDefault();
      pinned = true;
      closeButton.focus({preventScroll:true});
    });
  }
  preview.addEventListener('pointerenter', () => clearTimeout(closeTimer));
  preview.addEventListener('pointerleave', leave);
  preview.addEventListener('focusout', event => {
    if (!preview.contains(event.relatedTarget) && event.relatedTarget !== trigger) hide();
  });
  closeButton.addEventListener('click', () => hide(true));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && trigger) {
      const restore = preview.contains(document.activeElement);
      hide(restore);
      event.preventDefault();
    }
  });
  document.addEventListener('pointerdown', event => {
    if (trigger && !preview.contains(event.target) && !trigger.contains(event.target)) hide();
  });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', event => {
    if (!trigger || preview.contains(event.target instanceof Node ? event.target : null)) return;
    const rect = trigger.getBoundingClientRect();
    if (!pinned && (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth)) hide();
    else position();
  }, true);
})();
