'use strict';
(() => {
  const dialog = document.getElementById('pod-preview');
  if (!dialog?.showModal) return;
  const content = dialog.querySelector('[data-pod-content]');
  let request;
  document.getElementById('pod-list').addEventListener('click', async event => {
    const link = event.target.closest('a[data-pod-preview]');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.textContent = 'Loading POD…';
    content.replaceChildren(status);
    dialog.showModal();
    try {
      const response = await fetch(link.dataset.podPreview, {signal: controller.signal});
      if (!response.ok) throw new Error();
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const record = doc.querySelector('[data-pod-record]');
      if (!record) throw new Error();
      if (!controller.signal.aborted) content.replaceChildren(record);
    } catch {
      if (controller.signal.aborted) return;
      const fallback = document.createElement('a');
      fallback.href = link.href;
      fallback.textContent = 'Open record';
      status.textContent = 'The preview could not be loaded. ';
      status.append(fallback);
    }
  });
  dialog.addEventListener('close', () => { request?.abort(); content.replaceChildren(); });
})();
