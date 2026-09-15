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
    const target = document.getElementById(id); target.replaceChildren();
    items.forEach(([label,value]) => {const card=document.createElement('div');const title=document.createElement('span');title.textContent=label;const amount=document.createElement('strong');amount.textContent=value;card.append(title,amount);target.append(card);});
  }
  function calculate(edited=false) {
    const inputs = Array.from(form.querySelectorAll('input[type=number]'));
    const totalArea = rows.reduce((s,row)=>s+Number(row.querySelector('.monthly-area').value),0);
    const valid = inputs.every(el=>el.value!=='' && el.validity.valid) && totalArea<=40000;
    document.getElementById('save-monthly').disabled=!valid;
    const status=document.getElementById('monthly-status');
    if (!valid) {
      status.textContent = totalArea>40000 ? 'Total new rented area exceeds 40,000 sqm. Reduce an entry.' : 'Complete all months: new area 0–40,000 sqm and annual rate $5–$45.';
      rows.forEach(row=>row.querySelectorAll('[data-output]').forEach(el=>el.textContent='—'));
      for(let y=1;y<=4;y++){cards(`monthly-year-summary-${y}`,[]);document.getElementById(`monthly-year-caption-${y}`).textContent='Check inputs';}
      cards('monthly-overall',[['Forecast','Check inputs above']]);return;
    }
    status.textContent=edited?'Unsaved changes — save the forecast to keep them.':'Forecast loaded. Save to keep edits.';
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
  }
  form.addEventListener('input',()=>calculate(true));
  calculate();
})();
