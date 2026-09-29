window.BlocktexxPricing=(()=>{
 'use strict';
 let policy=typeof document!=='undefined'?JSON.parse(document.getElementById('bx-private-pricing')?.textContent||'{}'):{};
 const valid=n=>typeof n==='number'&&Number.isFinite(n);
 const money=n=>valid(n)?'$'+n.toLocaleString('en-AU',{minimumFractionDigits:2,maximumFractionDigits:2}):'Not available';
 function calculate(a,p=policy){
  if(!p.configured||!['administration_pct','profit_pct','minimum_pct','gst_pct'].every(k=>valid(p[k])&&p[k]>=0)||p.administration_pct+p.profit_pct+1e-9<p.minimum_pct)return null;
  const factor=1+(p.administration_pct+p.profit_pct)/100;
  const total=a.total*factor, gst=total*p.gst_pct/100, baseRate=a.kg>0?a.total/a.kg:null;
  const states=a.states.map(s=>{const freight=a.kg>0&&valid(s.kg)?a.interstate*s.kg/a.kg:0;const base=s.local+s.rental+s.equipment+freight;return {...s,freight,base,total:base*factor,rate:s.kg>0?base*factor/s.kg:null};});
  return {factor,total,gst,includingGST:total+gst,rate:baseRate==null?null:baseRate*factor,includingGSTRate:baseRate==null?null:baseRate*factor*(1+p.gst_pct/100),administration:a.total*p.administration_pct/100,profit:a.total*p.profit_pct/100,states,unallocatedFreight:a.kg>0?0:a.interstate*factor,transport:a.transport*factor,rental:a.rental*factor,equipment:a.equipment*factor,interstate:a.interstate*factor,local:a.local*factor};
 }
 function renderSettings(root,a){
  const e=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
  root.append(e('h3','Private administration and profit settings'));
  if(!policy.configured){root.append(e('p','Private pricing has not been configured. Provisional selling amounts remain unavailable.'));return;}
  root.append(e('p','These percentages are added to the same operating cost base, not compounded. Storage and repacking are separate. Settings are excluded from model JSON/CSV and response copy/print. All signed-in administrators can access this section.'));
  const form=e('form');form.className='bx-private-form';
  const inputs={};for(const [key,title] of [['administration_pct','Administration markup %'],['profit_pct','Profit markup %']]){const label=e('label',title),input=e('input');input.type='number';input.min='0';input.max='1000';input.step='0.1';input.value=policy[key];input.setAttribute('aria-label',title);label.append(input);form.append(label);inputs[key]=input;}
  const status=e('p');status.setAttribute('role','status');
  const preview=e('p');function updatePreview(){const combined=Number(inputs.administration_pct.value)+Number(inputs.profit_pct.value);preview.textContent='Combined markup: '+combined.toFixed(1)+'% · minimum '+policy.minimum_pct+'%. A cost of $100 produces '+money(100*(1+combined/100))+' before GST.';}
  Object.values(inputs).forEach(i=>i.oninput=updatePreview);updatePreview();
  form.append(preview);const save=e('button','Save private pricing');save.type='submit';save.className='primary';form.append(save,status);root.append(form);
  const values=calculate(a);if(values)root.append(e('p','Monthly administration allowance: '+money(values.administration)+' · monthly profit allowance: '+money(values.profit)+'. Profit allowance as a share of proposed sales: '+(values.total?values.profit/values.total*100:0).toFixed(2)+'%. Based on known costs only.'));
  form.onsubmit=async event=>{event.preventDefault();if(!form.reportValidity())return;const next={...policy,administration_pct:Number(inputs.administration_pct.value),profit_pct:Number(inputs.profit_pct.value)};if(next.administration_pct+next.profit_pct+1e-9<policy.minimum_pct){status.textContent='Not saved: combined markup must be at least '+policy.minimum_pct+'%.';return;}save.disabled=true;status.textContent='Saving private pricing…';
   try{const response=await fetch('/admin/blocktexx/private-pricing',{method:'POST',body:new URLSearchParams({csrf:document.getElementById('blocktexx').dataset.csrf,revision:String(policy.revision),administration_pct:String(next.administration_pct),profit_pct:String(next.profit_pct)})});if(!response.headers.get('content-type')?.includes('application/json'))throw Error('Session expired or service unavailable.');const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'Save failed.');policy=result.pricing;document.dispatchEvent(new CustomEvent('bx-pricing-changed'));}
   catch(error){status.textContent='Not saved: '+error.message;save.disabled=false;}
  };
 }
 return {calculate,renderSettings};
})();
