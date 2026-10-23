'use strict';
function shiftTimer(started, breakStarted, breakFinished, now) {
  const elapsed=Math.max(0,Math.floor((now-Date.parse(started))/1000));
  const takingBreak=Boolean(breakStarted && !breakFinished);
  const remaining=takingBreak?Math.max(0,1800-Math.floor((now-Date.parse(breakStarted))/1000)):0;
  const format=seconds=>[Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');
  return {elapsed:format(elapsed),due:elapsed>=14400&&!breakFinished&&!takingBreak,takingBreak,remaining,breakTime:format(remaining).slice(3)};
}
if(typeof module!=='undefined') module.exports={shiftTimer};
if(typeof document!=='undefined') (()=>{
  const clock=document.querySelector('[data-shift-clock]');
  if(!clock)return;
  const banner=document.querySelector('[data-break-banner]');
  const start=banner.querySelector('[data-break-start]');
  const finish=banner.querySelector('[data-break-finish]');
  const title=banner.querySelector('[data-break-title]');
  const message=banner.querySelector('[data-break-message]');
  let server=Date.parse(clock.dataset.serverNow),baseline=performance.now(),alerted=false,pending=false;
  function update(){
    const state=shiftTimer(clock.dataset.started,clock.dataset.breakStarted,clock.dataset.breakFinished,server+performance.now()-baseline);
    clock.querySelector('[data-elapsed]').textContent=state.elapsed;
    clock.querySelector('[data-break-summary]').textContent=state.takingBreak?'Break · '+state.breakTime:clock.dataset.breakFinished?'30-minute break completed':state.due?'Break due now':'Break due at 4 hours';
    clock.classList.toggle('break-due',state.due);
    banner.hidden=!(state.due||state.takingBreak);
    start.hidden=!state.due;finish.hidden=!state.takingBreak;
    finish.querySelector('button').disabled=state.remaining>0;
    title.textContent=state.takingBreak?(state.remaining?'Break · '+state.breakTime+' remaining':'Your 30-minute break is complete'):'Time for your 30-minute break';
    message.textContent=state.takingBreak?'Your break time is saved. Tap below when you are ready to continue.':'You started four hours ago. Park safely before taking your break.';
    if(state.due&&!alerted&&!document.hidden){alerted=true;navigator.vibrate?.([180,100,180]);}
  }
  async function sync(){
    if(document.hidden||pending)return;
    pending=true;
    try{
      const response=await fetch(clock.dataset.clockState,{headers:{Accept:'application/json'}});
      if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))return;
      const data=await response.json();
      if(!data.day){location.reload();return;}
      const changed=clock.dataset.breakStarted!==(data.day.break_started_at||'')||clock.dataset.breakFinished!==(data.day.break_finished_at||'');
      clock.dataset.breakStarted=data.day.break_started_at||'';clock.dataset.breakFinished=data.day.break_finished_at||'';
      server=Date.parse(data.server_now);baseline=performance.now();update();
      if(changed)location.reload();
    }catch(_){/* The persisted timestamp still drives the visible timer offline. */}
    finally{pending=false;}
  }
  update();setInterval(update,1000);setInterval(sync,30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){update();sync();}});
  window.addEventListener('pageshow',sync);
})();
