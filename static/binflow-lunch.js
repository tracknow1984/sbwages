'use strict';
(() => {
  const clock = document.querySelector('[data-shift-clock]');
  if (!clock) return;
  const settings = document.querySelector('.bf-alert-settings');
  const status = settings.querySelector('[data-sound-status]');
  const enable = settings.querySelector('[data-enable-push]');
  let audio;
  function unlock() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) return;
    audio ||= new Audio();
    if (audio.state !== 'running') audio.resume().catch(()=>{});
  }
  function chime() {
    if (!audio || audio.state !== 'running') return false;
    [660,880,660].forEach((tone, i) => {
      const start = audio.currentTime + i * .34;
      const note = audio.createOscillator(), gain = audio.createGain();
      note.frequency.value = tone;
      gain.gain.setValueAtTime(.001,start);
      gain.gain.exponentialRampToValueAtTime(.18,start+.025);
      gain.gain.exponentialRampToValueAtTime(.001,start+.27);
      note.connect(gain);gain.connect(audio.destination);
      note.start(start);note.stop(start+.28);
    });
    return true;
  }
  const lunchKey = ['binflow-lunch',clock.dataset.user,clock.dataset.day,clock.dataset.started,clock.dataset.clockState].join(':');
  let sounded = false;
  function ringWhenDue() {
    if (document.hidden || !clock.classList.contains('break-due') || sounded) return;
    try {if (localStorage.getItem(lunchKey)) return;} catch (_) {}
    if (chime()) {
      sounded = true;
      try {localStorage.setItem(lunchKey,'1');} catch (_) {}
    }
  }
  function activate() {
    unlock();
    if (audio) audio.addEventListener('statechange',ringWhenDue,{once:true});
    ringWhenDue();
  }
  document.addEventListener('click',activate,{once:true,capture:true});
  document.addEventListener('keydown',activate,{once:true,capture:true});
  clock.addEventListener('lunchdue',ringWhenDue);
  settings.querySelector('[data-test-sound]').addEventListener('click',async () => {
    unlock();
    try {
      if (audio) await audio.resume();
      status.textContent = chime() ? 'Test chime played.' : 'Sound unavailable in this browser.';
    } catch (_) {status.textContent='Check device sound settings.';}
  });
  if (!enable) return;
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!supported) {
    enable.hidden=true;
    status.textContent='For phone alerts, open BinFlow from your Home Screen in a supported browser.';
    return;
  }
  settings.querySelector('summary').textContent='Lunch alerts · set up';
  async function registerPhone(allowNew) {
    const reg = await navigator.serviceWorker.register('/binflow/driver/lunch-worker.js');
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub && !allowNew) return;
    if (!sub) {
      const response = await fetch(clock.dataset.alertUrl,{headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error();
      const {key} = await response.json();
      const binary = atob(key.replace(/-/g,'+').replace(/_/g,'/').padEnd(Math.ceil(key.length/4)*4,'='));
      sub = await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:Uint8Array.from(binary,c=>c.charCodeAt(0))});
    }
    const response = await fetch(clock.dataset.alertUrl,{method:'POST',headers:{Accept:'application/json'},
      body:new URLSearchParams({csrf:clock.dataset.csrf,endpoint:sub.endpoint})});
    if (!response.ok) throw new Error();
    settings.querySelector('summary').textContent='Lunch alerts · on';
    enable.hidden=true;
    status.textContent='Phone reminders enabled. Sound follows device notification settings.';
  }
  enable.addEventListener('click',async () => {
    enable.disabled=true;
    try {
      const permission=await Notification.requestPermission();
      if (permission==='granted') await registerPhone(true);
      else status.textContent='Allow notifications in your browser settings for lock-screen alerts.';
    } catch (_) {status.textContent='Could not enable phone alerts. Try again.';}
    finally {enable.disabled=false;}
  });
  if (Notification.permission==='granted') registerPhone(false).catch(()=>{});
})();
