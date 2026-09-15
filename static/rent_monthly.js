(() => {
  'use strict';
  const form = document.getElementById('monthly-form');
  if (!form) return;
  const rows = Array.from(form.querySelectorAll('.monthly-row'));
  const money = new Intl.NumberFormat('en-AU', {style:'currency',currency:'AUD'});
  const number = new Intl.NumberFormat('en-AU');
  // Amounts use annual cents as the numerator of monthly cents / 12.
  const dollars = numerator => money.format(numerator / 1200);
  const bill = 100000000;
  function cards(id, items) {
    const target = document.getElementById(id);
    items.forEach(([label,value],i) => {
      let card=target.children[i];
      if(!card){card=document.createElement('div');card.append(document.createElement('span'),document.createElement('strong'));target.append(card);}
      card.children[0].textContent=label;card.children[1].textContent=value;
    });
  }
  function calculate(edited=false) {
    const inputs = Array.from(form.querySelectorAll('input[type=number]'));
    const totalArea = rows.reduce((s,row)=>s+Number(row.querySelector('.monthly-area').value),0);
    const valid = inputs.every(el=>el.value!=='' && el.validity.valid) && totalArea<=40000;
    document.getElementById('save-monthly').disabled=!valid;
    const validation=document.getElementById('monthly-validation');
    inputs.forEach(el=>el.setAttribute('aria-invalid', String(el.value==='' || !el.validity.valid)));
    if (!valid) {
      const bad=inputs.find(el=>el.value==='' || !el.validity.valid);
      const month=bad?.closest('.monthly-row')?.dataset.month;
      validation.textContent = totalArea>40000 ? `You have entered ${number.format(totalArea)} new sqm, above the 40,000 sqm block limit. Enter only additional sqm each month. Your entries are retained; summaries show the last valid forecast.` : `Complete the input${month ? ' in Month '+month : ''}. Your entries are retained; summaries show the last valid forecast.`;
      return false;
    }
    validation.textContent='';
    let occupied=0,annualIncome=0,cumulative=0,firstOperating=null,firstCumulative=null,peakFunding=0;
    let allIncome=0,allSubsidy=0,allSurplus=0;
    let yearIncome=0,yearSubsidy=0,yearSurplus=0;
    rows.forEach((row,i)=>{
      const newArea=Number(row.querySelector('.monthly-area').value);
      const rateCents=Math.round(Number(row.querySelector('.monthly-rate').value)*100);
      occupied+=newArea;annualIncome+=newArea*rateCents;
      const net=annualIncome-bill;cumulative+=net;
      if(firstOperating===null && net>=0)firstOperating=i+1;
      if(firstCumulative===null && cumulative>=0)firstCumulative=i+1;
      peakFunding=Math.max(peakFunding,-cumulative);
      allIncome+=annualIncome;yearIncome+=annualIncome;
      const subsidy=Math.max(0,-net),surplus=Math.max(0,net);
      allSubsidy+=subsidy;yearSubsidy+=subsidy;allSurplus+=surplus;yearSurplus+=surplus;
      row.querySelector('[data-output=area]').textContent=number.format(occupied);
      row.querySelector('[data-output=income]').textContent=dollars(annualIncome);
      row.querySelector('[data-output=net]').textContent=net<0?`${dollars(-net)} subsidy`:net>0?`${dollars(net)} surplus`:'Break-even';
      row.querySelector('[data-output=balance]').textContent=dollars(cumulative);
      if((i+1)%12===0){
        const year=(i+1)/12;
        document.getElementById(`monthly-year-caption-${year}`).textContent=`Income ${dollars(yearIncome)} · Subsidy ${dollars(yearSubsidy)} · ${number.format(occupied)} sqm rented at year end`;
        cards(`monthly-year-summary-${year}`,[['Year rental income',dollars(yearIncome)],['Year fixed rent',money.format(1000000)],['Year subsidy required',dollars(yearSubsidy)],['Year surplus',dollars(yearSurplus)],['Year net result',dollars(yearIncome-12*bill)],['Cumulative balance',dollars(cumulative)]]);
        yearIncome=0;yearSubsidy=0;yearSurplus=0;
      }
    });
    cards('monthly-overall',[
      ['Monthly rent bill',dollars(bill)],['Monthly operating break-even',firstOperating?`Month ${firstOperating}`:'Not reached in 48 months'],['Cumulative break-even',firstCumulative?`Month ${firstCumulative}`:'Not reached in 48 months'],
      ['Total 48-month rental income',dollars(allIncome)],['Total 48-month rent bill',money.format(4000000)],['Total subsidies required',dollars(allSubsidy)],['Total monthly surpluses',dollars(allSurplus)],['Net 48-month result',dollars(cumulative)],['Peak funding required',dollars(peakFunding)],['Rented area by Month 48',`${number.format(occupied)} sqm`]
    ]);
    return true;
  }
  const inputs = Array.from(form.querySelectorAll('input[type=number]'));
  const status = document.getElementById('monthly-status');
  const draftKey = `sbwages:monthly-rent:${form.dataset.user}:v1`;
  const snapshot = () => Object.fromEntries(inputs.map(el => [el.name, el.value]));
  const normalise = values => JSON.stringify(inputs.map(el => {
    const value = values[el.name];
    return value === '' || value === undefined ? null : Number(value);
  }));
  let revision = form.dataset.revision;
  let dirty = false, localStored = false, saving = false, blocked = false, timer = null;
  let pendingRestore = null;
  const persistDraft = () => {
    try {
      localStorage.setItem(draftKey, JSON.stringify({values:snapshot(), revision,
        openYears:Array.from(form.querySelectorAll('.monthly-year')).map(el=>el.open)}));
      localStored = true;
    } catch (_) { localStored = false; }
  };
  const applyDraft = draft => {
    inputs.forEach(el => {if(typeof draft.values[el.name] === 'string') el.value = draft.values[el.name];});
    form.querySelectorAll('.monthly-year').forEach((el,i)=>{if(draft.openYears)el.open=!!draft.openYears[i];});
    dirty = true; persistDraft(); calculate();
  };
  function scheduleSave() {
    clearTimeout(timer);
    timer = setTimeout(save, 700);
  }
  async function save() {
    clearTimeout(timer);
    if (blocked || saving || !dirty || !calculate()) return;
    saving = true;
    const sent = snapshot();
    const body = new FormData(form); body.set('revision',revision);
    status.textContent = 'Saving forecast…';
    let succeeded = false;
    try {
      const response = await fetch(form.action, {method:'POST', body, headers:{Accept:'application/json'}, signal:AbortSignal.timeout(15000)});
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Save failed or your session expired. Sign in again if needed; your draft is retained.');
      const result = await response.json();
      if (!response.ok || !result.ok) {
        blocked = response.status === 409;
        throw new Error(result.error || 'Unable to save. Your draft is retained.');
      }
      revision = result.revision;
      succeeded = true;
      if (JSON.stringify(sent) === JSON.stringify(snapshot())) {
        dirty = false;
        try {localStorage.removeItem(draftKey);} catch (_) {}
        status.textContent = 'All 48 months saved.';
      } else {
        persistDraft();
        status.textContent = 'Saving your latest changes…';
      }
    } catch (error) {
      persistDraft();
      status.textContent = `${error.message} ${localStored ? 'Draft kept on this browser.' : 'Keep this page open and retry Save now.'}`;
    } finally {
      saving = false;
      if (succeeded && dirty && calculate()) scheduleSave();
    }
  }
  form.addEventListener('input', event => {
    if (!event.target.matches('input[type=number]')) return;
    dirty = true;
    persistDraft();
    const valid = calculate();
    if (!blocked) status.textContent = localStored ? 'Draft retained on this browser. ' + (valid ? 'Saving shortly…' : 'Complete the highlighted input to save online.') : 'Unsaved changes — keep this page open until saved.';
    if (valid) scheduleSave();
    else clearTimeout(timer);
  });
  form.addEventListener('submit', event => {event.preventDefault();save();});
  form.addEventListener('keydown', event => {
    if(event.key === 'Enter' && event.target.matches('input[type=number]')) {event.preventDefault();save();}
  });
  window.addEventListener('online',()=>{if(dirty && !blocked)save();});
  window.addEventListener('beforeunload',event=>{
    if(dirty && !localStored){event.preventDefault();event.returnValue='';}
  });
  document.getElementById('restore-monthly-draft').addEventListener('click',()=>{
    if(!pendingRestore)return;
    applyDraft(pendingRestore);pendingRestore=null;blocked=false;
    document.getElementById('restore-monthly-draft').hidden=true;
    status.textContent='Local draft restored for review. Select Save now to replace the server forecast.';
  });
  calculate();
  try {
    const draft=JSON.parse(localStorage.getItem(draftKey) || 'null');
    if(draft?.values && typeof draft.values === 'object') {
      if(normalise(draft.values) === normalise(snapshot())) {
        localStorage.removeItem(draftKey);
        status.textContent='All 48 months loaded from your saved forecast.';
      } else if(draft.revision === revision) {
        applyDraft(draft);
        status.textContent='Your unfinished draft has been restored.';
        if(calculate())scheduleSave();
      } else {
        pendingRestore=draft;
        document.getElementById('restore-monthly-draft').hidden=false;
        status.textContent='The server forecast changed. Your local draft is retained; restore it only if you want to use those edits.';
      }
    }
  } catch (_) {status.textContent='Browser draft storage unavailable. Keep this page open until changes are saved online.';}

})();
