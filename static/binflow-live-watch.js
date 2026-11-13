(() => {
  const records = document.querySelector('[data-live-watch]');
  const status = document.querySelector('[data-watch-status]');
  if (!records || !status) return;
  let fetching = false;
  async function refresh() {
    if (fetching || document.hidden) return;
    fetching = true;
    try {
      const response = await fetch(location.href, {cache: 'no-store'});
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = html.querySelector('[data-live-watch]');
      if (!response.ok || !next) throw new Error();
      if (records.innerHTML !== next.innerHTML) records.replaceChildren(...Array.from(next.childNodes));
      status.textContent = 'Updated ' + new Date().toLocaleTimeString('en-AU') + ' · checks every 10 seconds';
    } catch {
      status.textContent = 'Update interrupted. Showing the last records. Retrying in 10 seconds.';
    } finally {
      fetching = false;
    }
  }
  window.setInterval(refresh, 10000);
  document.addEventListener('visibilitychange', refresh);
})();
