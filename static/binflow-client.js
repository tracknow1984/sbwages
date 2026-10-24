/* Refresh collection progress without moving the reader or reopening closed days. */
(() => {
  if (!document.querySelector('.bf-client-week')) return;
  let busy = false;
  window.setInterval(async () => {
    if (document.hidden || busy) return;
    busy = true;
    try {
      const response = await fetch(window.location.href, {credentials: 'same-origin'});
      if (!response.ok || response.redirected) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('.bf-client-week');
      const current = document.querySelector('.bf-client-week');
      if (!next || !current || current.contains(document.activeElement)) return;
      current.querySelectorAll('details').forEach((day, index) => {
        const replacement = next.querySelectorAll('details')[index];
        if (replacement) replacement.open = day.open;
      });
      current.replaceWith(next);
    } catch (_) { /* Keep the last visible progress if connectivity drops. */ }
    finally { busy = false; }
  }, 20000);
})();
