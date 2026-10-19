'use strict';
(() => {
  const stock = document.querySelector('[data-driver-stock]');
  if (!stock) return;
  const form = document.querySelector('[data-scan-form]');
  let busy = false;
  async function refreshStock() {
    if (busy || document.hidden) return;
    try {
      const response = await fetch(stock.dataset.stateUrl, {headers: {Accept: 'application/json'}, cache: 'no-store'});
      if (response.ok && response.headers.get('content-type')?.includes('application/json')) {
        const data = await response.json();
        if (!busy) stock.innerHTML = data.html;
      }
    } catch (_) { /* Keep the last confirmed manifest while connectivity returns. */ }
  }
  window.setInterval(refreshStock, 20000);
  if (!form) return;
  const input = form.querySelector('[data-scan-code]');
  const feedback = form.querySelector('[data-scan-feedback]');
  const context = form.querySelector('[data-scan-context]');
  const video = form.querySelector('[data-camera]');
  const start = form.querySelector('[data-camera-start]');
  const stop = form.querySelector('[data-camera-stop]');
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  let stream = null, timer = null, lastCode = '', lastAt = 0, opening = false, cameraGeneration = 0;
  function say(message, error = false) {
    feedback.textContent = message;
    feedback.classList.toggle('is-error', error);
  }
  function stopCamera() {
    cameraGeneration++;
    window.clearTimeout(timer);
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null;
    video.srcObject = null;
    video.hidden = stop.hidden = true;
    start.hidden = false;
  }
  function setAction() {
    const selected = form.querySelector('[name="action"]:checked');
    context.textContent = context.dataset[selected.value];
    lastCode = '';
    say('Ready. Scan one bin at a time for this movement.');
  }
  form.querySelectorAll('[data-scan-action]').forEach(radio => radio.addEventListener('change', setAction));
  setAction();
  async function scan(code) {
    if (busy || !code.trim()) return;
    busy = true;
    input.value = code;
    const data = new FormData(form);
    const buttons = [...form.querySelectorAll('button[type="submit"],input[name="action"]')];
    buttons.forEach(b => { b.disabled = true; });
    say('Saving scan…');
    try {
      const response = await fetch(form.getAttribute('action'), {method: 'POST', body: data, headers: {Accept: 'application/json'}});
      if (!response.headers.get('content-type')?.includes('application/json')) {
        stopCamera();
        throw new Error('Your session may have expired. Refresh and sign in, then scan this bin again.');
      }
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Scan could not be saved. Try again.');
      stock.innerHTML = result.html;
      say(result.message);
      input.value = '';
      if (!result.duplicate && navigator.vibrate) navigator.vibrate(80);
    } catch (error) {
      say(error instanceof TypeError ? 'Connection lost. Check your connection and scan this bin again to confirm its location.' : error.message, true);
    } finally {
      busy = false;
      buttons.forEach(b => { b.disabled = false; });
      if (!stream) input.focus();
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); scan(input.value); });
  function frame() {
    if (!stream) return;
    if (!busy && video.readyState >= 2 && video.videoWidth) {
      const scale = Math.min(1, 720 / video.videoWidth);
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = window.jsQR(pixels.data, pixels.width, pixels.height, {inversionAttempts: 'dontInvert'});
      if (code && (code.data !== lastCode || Date.now() - lastAt > 5000)) {
        lastCode = code.data;
        lastAt = Date.now();
        scan(code.data);
      }
    }
    timer = window.setTimeout(frame, 250);
  }
  start.addEventListener('click', async () => {
    if (opening || stream) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.jsQR) {
      say('Camera scanning is unavailable here. Use a handheld scanner or enter the bin ID below.', true);
      return;
    }
    opening = true;
    const generation = cameraGeneration;
    try {
      const camera = await navigator.mediaDevices.getUserMedia({video: {facingMode: {ideal: 'environment'}, width: {ideal: 1280}}, audio: false});
      if (generation !== cameraGeneration || document.hidden) {
        camera.getTracks().forEach(track => track.stop());
        return;
      }
      stream = camera;
      video.srcObject = stream;
      video.hidden = stop.hidden = false;
      start.hidden = true;
      await video.play();
      say('Point the camera at a bin QR label. Keep this movement selected as you scan the next bin.');
      frame();
    } catch (_) {
      stopCamera();
      say('Could not open the camera. Allow camera access in your browser, or enter the bin ID below.', true);
    } finally { opening = false; }
  });
  stop.addEventListener('click', stopCamera);
  window.addEventListener('pagehide', stopCamera);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); else refreshStock(); });
})();
