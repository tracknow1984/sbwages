(() => {
  'use strict';
  const link = document.getElementById('xero-authorize');
  if (!link) return;
  try {
    const destination = new URL(link.href);
    if (destination.origin !== 'https://login.xero.com' ||
        destination.pathname !== '/identity/connect/authorize' ||
        destination.username || destination.password) return;
    window.location.replace(destination.href);
  } catch (_) {
    // Keep the normal link usable if automatic navigation is unavailable.
  }
})();
