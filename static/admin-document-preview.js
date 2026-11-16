(() => {
  const dialog = document.querySelector('.register-preview');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const body = dialog.querySelector('.preview-body');
  dialog.addEventListener('close', () => body.replaceChildren());

  // Transfer calendar cards insert their preview links when the card is opened.
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-preview]');
    if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const pdf = link.dataset.preview === 'application/pdf';
    const preview = document.createElement(pdf ? 'iframe' : 'img');
    preview.setAttribute(pdf ? 'title' : 'alt', link.dataset.name);
    preview.src = link.href;
    dialog.querySelector('#preview-name').textContent = link.dataset.name;
    dialog.querySelector('[data-open-preview]').href = link.href;
    body.replaceChildren(preview);
    dialog.showModal();
  });
})();
