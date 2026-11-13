(() => {
  const dialog = document.querySelector('.register-preview');
  const body = dialog.querySelector('.preview-body');
  dialog.addEventListener('close', () => body.replaceChildren());
  document.querySelectorAll('[data-preview]').forEach(link => {
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !dialog.showModal) return;
      event.preventDefault();
      const pdf = link.dataset.preview === 'application/pdf';
      const preview = document.createElement(pdf ? 'iframe' : 'img');
      preview.setAttribute(pdf ? 'title' : 'alt', link.dataset.name);
      preview.src = link.href;
      dialog.querySelector('#preview-name').textContent = link.dataset.name;
      body.replaceChildren(preview);
      dialog.showModal();
    });
  });
})();
