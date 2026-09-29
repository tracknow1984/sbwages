/* RFI responses are derived from the current model; source figures are private runtime data. */
window.BlocktexxRFI = (() => {
  'use strict';
  const states = ['ACT','NSW','QLD','SA','TAS','VIC','WA'];
  const valid = n => typeof n === 'number' && Number.isFinite(n);
  const sum = values => values.reduce((n,v) => n + (valid(v) ? v : 0), 0);
  const num = n => valid(n) ? n.toLocaleString('en-AU',{maximumFractionDigits:1}) : 'Not entered';
  const money = n => valid(n) ? '$'+n.toLocaleString('en-AU',{minimumFractionDigits:2,maximumFractionDigits:2}) : 'Not quoted';
  const rate = n => valid(n) ? '$'+n.toFixed(3)+'/kg' : 'Not quoted';
  const required = 'BlockTexx clarification required';
  const q = (...ids) => ids.map(n => 'q'+String(n).padStart(3,'0'));
  // Every requested bullet has its own live answer, supporting clarifications and saved supplement.
  const definitions = [
    ['Pricing schedule',[
      ['Scheduled collection rate',q(1,10,81,82,89),'Use the state selling rates below as draft rates only. Confirm the chargeable kilogram and included services before issuing a quote.','pricing'],
      ['Ad hoc collection rate',q(26,58,84),'Ad hoc work is proposed to be separately quoted after checking route, vehicle, loading requirements and available capacity. No customer ad hoc rate is recorded.'],
      ['Bin/cage rental or supply cost',q(47,48,49,50,51,52),'Current resource quantities and unit amounts are shown below. The existing resource figures are model inputs; confirm whether they are supplier costs or agreed customer charges.','resources'],
      ['Swap/retrieval fees',q(22,51,53),'Proposed operating method: exchange full containers for equivalent empty containers and reconcile quantities at each handover. A separate swap/retrieval selling fee has not been recorded.'],
      ['Depot handling/storage cost',q(2,40,41,45,72,75,79,80),'Storage and one-off repacking are shown separately. Routine depot time is included only where recorded in the route; additional handling and baling charges require confirmation.','storage'],
      ['Linehaul rate',q(54,55,56,57,58,59,60,61,62,63),'The current carrier lane inputs and booked departures are shown below. These are entered freight costs, not an approved customer rate card. Confirm any unpriced origin, part-load and empty-return movements.','linehaul'],
      ['Decommissioned stock transfer rate to ThreadTexx Rocklea',q(29,32,33,55,64,66,70),'Local decomm and production movements and interstate departures are included only when scheduled. ThreadTexx transfer selling rates and PEF routing approval remain to be confirmed.','movements'],
      ['Reporting/admin fees',q(83,84,95),'No separate reporting/admin selling fee is recorded. Confirm whether reporting is included in the agreed rate or charged separately; a blank amount is not a zero-dollar commitment.'],
      ['Fuel levy or CPI assumptions',q(61,85,86),'Lane-specific fuel percentages are shown with the freight inputs. Proposed price reviews should distinguish fuel movements from annual CPI and avoid applying a second levy to an already inclusive rate. The customer review mechanism is not agreed.'],
      ['Exclusions',q(1,2,3,33,36,84,89),'Proposed scope is logistics. Processing/shredding, decommissioning partner fees, waste disposal and unentered charges need explicit inclusion or exclusion. Storage and one-off repacking remain a separate cost centre.']
    ]],
    ['Volume forecast',[
      ['Expected monthly volume range by state',q(9,11,14,16),'The table distinguishes the RFI historical reference from current planner intake and any entered forecast range. Historical volumes are not guaranteed commitments. Missing state models remain explicit coverage gaps.','volumes'],
      ['Known ramp-up assumptions',q(8,15,18),'Proposed rollout starts with confirmed sites, equipment and partner capacity. Growth follows an agreed state-level forecast and mobilisation notice. No start date or growth commitment is assumed from historical activity.'],
      ['Pricing or service changes at 150, 250 and 400 tonne scenarios',q(15,18,30,31,41,42,50,54,56,59,85,94),'The scenarios below recalculate state allocations, indicative collection departures and equipment from the current mix. They are planning estimates, not proven capacity. Enter a proposed national rate and capacity changes for each scenario.','scenarios'],
      ['Seasonal or campaign-driven spikes',q(11,14,15,94),'Agree a campaign calendar, peak kg by state, equipment buffer, notice period and overflow carrier/depot plan. No seasonal uplift is assumed without evidence.']
    ]],
    ['Service expectations',[
      ['Booking lead times',q(19,20,26,32,63,68),'Proposed approach: maintain agreed recurring routes and confirm additional bookings against vehicle, depot and partner availability. Lead times in hours/days require agreement.'],
      ['Minimum response times',q(26,63,90),'Agree acknowledgement, dispatch and resolution targets separately, including operating hours and weekend/public-holiday coverage. No response-time SLA is currently committed.'],
      ['Missed collection process',q(26,27,90),'Proposed process: record the exception and reason, notify the nominated contact, agree a recovery booking, and retain completion evidence. Responsibility, recovery time and failed-service fees require agreement.'],
      ['Urgent/ad hoc collection expectations',q(26,58,84,94),'Proposed approach: accept urgent work subject to safe capacity and approval of the quoted variation. Confirm notice, cut-off, regional coverage and priority rules.'],
      ['Equipment replacement timeframe',q(48,50,51,52),'Use the resource register to plan exchange stock. Agree loss/damage responsibility, spare stock, cleaning and replacement deadlines; two sets alone do not prove a surge buffer.'],
      ['Depot storage limits',q(30,42,54,57,59,64,78,80),'Depot locations, recorded partner constraints and storage quantities are shown below. Floor-space, maximum kg, dwell time and overflow limits must be verified before committing capacity.','network'],
      ['Escalation process',q(6,7,37,70,90,96),'Proposed escalation: driver/site contact → state operations contact → national account manager → authorised BlockTexx decision-maker. Record names, phone numbers, operating hours and escalation deadlines before mobilisation.']
    ]],
    ['Chain-of-custody and evidence requirements',[
      ['Proof of collection',q(5,22,28),'Proposed record: unique job/batch ID, customer and location, collection date/time, driver, container IDs/counts and customer acknowledgement or documented exception. The planner itself is not proof of collection.'],
      ['Weight records',q(10,11,12,13,43),'Proposed record: gross, tare and net kg, weighing location/device, docket ID and calibration evidence. Reconcile collection, decomm return, bale and final-delivery weights; count intake once.'],
      ['Photos, where required',q(28,38,95),'Proposed evidence: time-stamped load, labels, seals and exception photos attached to the job/batch. Agree mandatory events, privacy controls and retention. Photo capture capability is not verified by this modelling page.'],
      ['Proof of delivery',q(5,38,63),'Proposed record: recipient, destination, date/time, batch/container quantities, condition and signed or electronic acceptance. Record shortages or refusal before closing the job.'],
      ['Transfer records to ThreadTexx',q(38,44,55,63,64,71),'Proposed pathway: customer → local depot → decomm partner → return/bale/consolidate → interstate transfer where required → ThreadTexx Rocklea → approved storage or production destination. Maintain one linked batch history through every handover. PEF diversion destinations require explicit approval.'],
      ['Monthly reconciliation report',q(11,13,17,44,71,83,95),'Proposed monthly report: opening stock + receipts − dispatches − authorised losses = closing stock; reconcile kg, equipment and exceptions by customer, state, batch and destination. Include on-time service, evidence completeness, weight coverage, exceptions, invoice accuracy and report timeliness; agree KPI targets and delivery deadline.']
    ]],
    ['Compliance and safety requirements',[
      ['Insurance cover',q(91),'Provide current certificates and policy limits for public liability, motor, workers compensation, goods in transit and stored stock as applicable. Insurance cost entries do not verify cover. Insurer, limits, expiry dates and exclusions require evidence.'],
      ['WHS procedures',q(92,93),'Proposed submission: relevant WHS procedures, risk assessments, manual handling, loading/unloading, forklift/baler controls and incident response. Confirm documented procedures and site inductions before commencement.'],
      ['Driver/site safety processes',q(23,24,46,93,94),'Proposed controls: appropriate licences, induction, prestarts, fatigue management, safe access, load restraint, vehicle suitability and defect escalation. Confirm each site and contractor meets the agreed requirements.'],
      ['Chain-of-custody controls',q(5,28,38,44,95),'Proposed controls: batch labels, handover checks, controlled access, exception logs, inventory reconciliation and authorised release. Operational evidence and audits are required; this proposal model does not certify compliance.'],
      ['Environmental compliance',q(3,13,36,78,92),'Confirm material classification, acceptance criteria, any applicable transport/storage approvals, spill/fire controls, rejection pathways and waste destination evidence for each facility. Do not assume textile or PEF stock follows one regulatory pathway.'],
      ['Subcontractor use and controls',q(4,29,55,60,91,93,96),'The proposed network uses company operations in QLD/NSW and partner depots/contractor trucks in VIC/SA/WA, subject to the current operator settings and scope confirmation. Verify carrier approvals, insurance, training, agreed evidence standards, audit rights and contingency capacity.','operators'],
      ['Modern slavery / ethical sourcing position',q(93,95,96),'Obtain SB Empire’s approved ethical sourcing/modern slavery position and subcontractor due-diligence evidence where required. No certification, policy or statutory reporting status is asserted.']
    ]],
    ['Assumptions and exclusions',[
      ['Minimum charges',q(9,21,82),'Current contractor minimums are operating-cost assumptions, not customer terms. Agree any minimum collection charge, monthly kg commitment or capacity reservation.','minimums'],
      ['Metro vs regional pricing',q(19,24,81,84),'Price the agreed sites and routes. Identify regional detours, distance bands, extra travel time and minimum loads separately before committing a statewide rate.'],
      ['Failed collections',q(26,27),'Agree customer-not-ready, no-access and cancellation charges, evidence requirements, notice periods and who authorises a return visit.'],
      ['Waiting time',q(25,60,70),'Use the current waiting allowances and demurrage assumptions below for modelling; customer billable allowances and rates require agreement.','waiting'],
      ['Contamination',q(13,36,92),'Agree acceptance standards, quarantine, rejection, disposal, extra handling and weight adjustments for wet, mouldy, hazardous or non-conforming stock.'],
      ['Fuel levy',q(60,61,86),'The lane table uses each entered fuel percentage. Agree the reference index, review frequency, base date and application to avoid double charging.'],
      ['Pallet/bulk load handling',q(18,23,35,39,45,46,67),'Confirm bale/pallet dimensions, weights, density, stackability, restraints, loading equipment and packaging. Extra handling or repacking requires a separately agreed allowance.'],
      ['Remote areas',q(4,19,24,55,81),'Unmodelled states/territories and remote destinations require route, carrier and depot confirmation. The current four-state financial model must not be represented as a fully priced national service.'],
      ['After-hours collections',q(19,26,84,93),'After-hours, weekend and public-holiday work requires confirmed access, safe staffing and agreed surcharges or a separate quote.']
    ]],
    ['Implementation plan',[
      ['Account setup timing',q(7,8,83,96),'Proposed gate 1: agree scope, commercial schedule, authorised contacts, purchase-order process and account setup before a start date is committed.'],
      ['Equipment rollout',q(39,40,47,48,50,52,53),'Proposed gate 2: confirm container quantities, exchange stock, depot handling equipment, ownership, labels and delivery dates. Resource requirements below update with the current model.'],
      ['Site onboarding',q(19,20,23,24,29,93),'Proposed gate 3: validate customer and decomm partner addresses, contacts, access, induction, frequency, load size and delivery windows; agree routes and recovery arrangements.'],
      ['Reporting setup',q(10,12,28,43,44,71,83,95),'Proposed gate 4: agree batch IDs, weighing and evidence fields, monthly report, KPI definitions, system access, data retention and invoice reconciliation.'],
      ['Trial period',q(38,46,74,90,96),'Proposed gate 5: run an agreed pilot through collection, decomm, consolidation, transfer and reconciliation. Confirm duration, acceptance criteria and measured route/handling times before full rollout.'],
      ['First month review',q(14,85,88,90,95,96),'Proposed gate 6: review actual kg, on-time service, equipment balance, depot dwell, linehaul utilisation, evidence completeness and invoices after the first operating month. Agree changes and accountable owners.'],
      ['Escalation contacts',q(6,7,37,70,96),'Nominate SB Empire national/state contacts, depot and carrier contacts, BlockTexx decision-makers and after-hours backups. Contact details are not inferred from unrelated staff or customer records.']
    ]],
    ['Contract terms',[
      ['Contract term',q(8,87),'Contract duration, commencement date, renewal and any minimum commitment remain subject to an agreed written contract.'],
      ['Review periods',q(85,86,90),'Proposed reviews: mobilisation/pilot acceptance, first operating month, then an agreed recurring operational and commercial review cycle. Confirm dates and variation authority.'],
      ['CPI / price review mechanism',q(61,85,86),'Agree the CPI series, base period, annual review date, treatment of negative movements and separate fuel/carrier/wage changes. No automatic percentage increase is assumed.'],
      ['Notice period',q(15,26,87),'Agree notice for scope changes, new sites, volume changes, rate changes, renewal and termination, including recovery of equipment and committed capacity.'],
      ['Confidentiality',q(95,96),'Proposed mutual confidentiality covering commercial rates, customer information, operations and RFI material. Agree permitted disclosures, subcontractor obligations and survival after termination.'],
      ['Data/reporting ownership',q(11,28,71,95),'Agree ownership, permitted use, access, export format, retention, privacy/security requirements and return or deletion of customer and chain-of-custody records at contract end.'],
      ['Termination rights',q(37,87,88,91),'Agree termination for cause/convenience, cure periods, outstanding charges, unrecovered mobilisation commitments and safe transfer of stock, records and equipment. Terms are subject to written agreement.'],
      ['Subcontractor approval',q(29,55,60,91,93,96),'Agree approval of nominated partner depots/carriers, substitution rules, responsibility for performance and flow-down of safety, insurance, confidentiality and evidence obligations.']
    ]]
  ];
  const table = (title,headers,rows) => ({title,headers,rows});
  function build(model, reference={}) {
    const national=window.BlocktexxNational.calculate(model), planner=window.createBlocktexxPlanner();
    const a=model.rfi||{}, bank=window.BlocktexxQuestions?.bank||[], answers=model.clarification_answers||{};
    const current = state => national.states.find(s=>s.state===state);
    const forecasts=states.map(state=>({state,reference:reference.states?.[state]?.kg??null,kg:current(state)?.kg??null,low:a.forecasts?.[state]?.low??null,high:a.forecasts?.[state]?.high??null}));
    const mix=forecasts.map(s=>({...s,basis:valid(s.kg)?'Current model':'RFI reference; state not modelled',weight:valid(s.kg)?s.kg:s.reference}));
    const mixTotal=sum(mix.map(s=>s.weight));
    const scopes = ['collection','decom','production'];
    const movementRows=[];
    for(const [state,d] of Object.entries(model.states)) for(const scope of scopes){
      const runs=scope==='collection'?d.runs.filter(r=>!r.activity_type||r.activity_type==='collection'):window.BlocktexxPlannerScope.runs(d.runs,scope);
      const slots=sum(runs.map(r=>planner.slots(r).length));
      movementRows.push([state,scope==='collection'?'Local collections':scope==='decom'?'Decomm transfers':'Production transfers',num(slots),num(slots*13/12),String(runs.filter(r=>r.runs_4w!==0&&!planner.slots(r).length).length)]);
    }
    const laneRows=Object.entries(model.interstate?.lanes||{}).map(([id,l])=>{
      const trips=sum((model.interstate.bookings||[]).filter(b=>b.lane_id===id).map(b=>b.trips))*13/12;
      const complete=['base_trip','fuel_pct','tolls_trip','other_trip'].every(k=>valid(l[k]));
      return [window.BlocktexxInterstate.lanes[id]?.[0]||id,money(l.base_trip),valid(l.fuel_pct)?num(l.fuel_pct)+'%':'Not entered',money(l.tolls_trip),money(l.other_trip),money(complete?l.base_trip*(1+l.fuel_pct/100)+l.tolls_trip+l.other_trip:null),num(trips)];
    });
    const notes=(keys)=>keys.filter(key=>model.process_flow_notes?.[key]?.trim()).map(key=>[key.replaceAll('_',' '),model.process_flow_notes[key]]);
    const tables={
      pricing:[table('Draft state selling rates · scope subject to agreement',['State','Entered selling rate · ex GST','Modelled intake kg/month','Basis'],states.map(s=>[s,rate(model.states[s]?.selling_per_kg),num(current(s)?.kg),current(s)?.basis||'Not modelled']))],
      resources:[table('Resources · live model inputs, not an approved selling schedule',['State','Equipment','Required: two sets','Rental qty','Input rental / each / week','Input supply cost / each'],national.resources.map(r=>{const key={ '120L bins':'bin120','240L bins':'bin240','660L bins':'bin660','Cages':'cage','Pallecons':'pallecon'}[r.label];const p=model.states[r.state].resource_pricing?.[key]||{};return [r.state,r.label,num(r.required),num(r.qty),money(p.weekly_rent_each),money(p.purchase_each)];}))],
      storage:[table('Storage · separate from operating rate',['Item','Model amount · ex GST'],[['Storage contract / month',money(national.storage)],['Repacking / one-off',money(national.repack)],['Retained containers',num(model.storage?.total_containers)],['Free containers',num(model.storage?.free_containers)]])],
      linehaul:[table('Linehaul · current carrier cost inputs',['Lane','Base / trip','Fuel levy','Tolls / trip','Other / trip','All-in / trip','Booked trips / month'],laneRows)],
      movements:[table('Scheduled activity · current four-week cycle',['State','Activity','Departures / 4 weeks','Departures / month','Unallocated rows'],movementRows)],
      volumes:[table('Monthly volume forecast · kg',['State','RFI historical reference','Current model intake','Expected minimum','Expected maximum','Coverage'],forecasts.map(s=>[s.state,num(s.reference),num(s.kg),num(s.low),num(s.high),current(s.state)?current(s.state).basis:'Not in financial model']))],
      network:[table('Depots and partner information',['State','Depot / status','Decomm partners','Recorded capacity','Recorded turnaround'],states.map(s=>{const d=model.states[s];return [s,d?`${d.depot||'Not entered'} / ${d.depot_status}`:'Not modelled',(model.partners||[]).filter(p=>p.state===s).map(p=>p.name+' — '+p.address).join('; ')||'Not entered',d?.decomm_questions?.capacity||'Not confirmed',d?.decomm_questions?.turnaround||'Not confirmed'];}))],
      operators:[table('Operator setting in current financial model',['State','Current operator setting'],states.map(s=>[s,model.states[s]?window.BlocktexxCosts.modeLabel(model.states[s].cost_mode):'Not modelled; scope confirmation required']))],
      minimums:[table('Contractor assumptions · customer terms require agreement',['State','Minimum hours','Pricing basis'],Object.entries(model.states).map(([s,d])=>[s,num(d.cost_profile?.enabled?d.cost_profile.minimum_hours:d.minimum_hours),d.cost_profile?.enabled?d.cost_profile.contractor_basis:'Aggregate']))],
      waiting:[table('Waiting assumptions · not approved customer charges',['State','Free wait minutes','Demurrage / hour'],Object.entries(model.states).map(([s,d])=>[s,num(d.cost_profile?.free_wait_minutes),money(d.cost_profile?.demurrage_hourly)]))]
    };
    const recordedNotes=notes(['dispatch','collect','weigh','decomm','bale','interstate','linehaul','shred','decision','storage','production','net_vic','net_sa','net_wa','net_nsw','net_qld','net_threshold','net_sydney','net_north','net_thread']);
    if(recordedNotes.length)tables.network.push(table('Recorded process notes · working assumptions',['Stage','Note'],recordedNotes));
    const scenarios=['150','250','400'].map(key=>{
      const config=a.scenarios?.[key]||{}, tonnes=config.tonnes??Number(key),target=tonnes*1000;
      const rows=mix.map(s=>{
        const kg=mixTotal>0&&valid(s.weight)?target*s.weight/mixTotal:null,base=current(s.state);
        const ratio=base?.kg>0&&valid(kg)?kg/base.kg:null;
        const qty=sum(national.resources.filter(r=>r.state===s.state).map(r=>r.required));
        return [s.state,num(kg),s.basis,valid(ratio)&&base.visits>0?num(Math.ceil(base.visits*ratio)):'Not established',valid(ratio)&&qty>0?num(Math.ceil(qty*ratio)):'Not established'];
      });
      return {key,tonnes,rate:config.rate??null,monthly:valid(config.rate)?target*config.rate:null,notes:config.notes||'',table:table(num(tonnes)+'t/month · illustrative capacity demand',['State','Allocated kg/month','Volume mix basis','Collection departures/month*','Exchange equipment units*'],rows)};
    });
    tables.scenarios=scenarios.map(s=>s.table);
    const sections=definitions.map(([title,items],i)=>({number:'11.'+(i+1),title,items:items.map(([title,ids,draft,key],j)=>{
      const id=(i+1)+'.'+(j+1), supplement=a.responses?.[id]||{};
      const linked=ids.map(id=>{const item=bank.find(q=>q.id===id);const answer=answers[id]||{};return {id,question:item?.question||id,answer:answer.answer||'',status:answer.status||'open'};});
      const open=linked.filter(x=>!(x.status==='not_applicable'||x.status==='answered'&&x.answer.trim()));
      return {id,title,draft,linked,open,supplement,tables:tables[key]||[]};
    })}));
    const issues=[...national.issues,...states.filter(s=>!model.states[s]).map(s=>s+' is not covered by the financial model.')];
    return {sections,scenarios,forecasts,mixTotal,national,issues,reference};
  }
  const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  function drawTable(root,t){root.append(e('h4',t.title));const wrap=e('div',null,'bx-scroll'),grid=e('table',null,'bx-resource-table'),head=e('thead'),tr=e('tr');t.headers.forEach(x=>tr.append(e('th',x)));head.append(tr);grid.append(head);const body=e('tbody');for(const row of t.rows){const tr=e('tr');row.forEach((x,i)=>tr.append(e(i?'td':'th',x)));body.append(tr);}grid.append(body);wrap.append(grid);root.append(wrap);if(!t.rows.length)root.append(e('p','No entries recorded. '+required+'.','bx-rfi-alert'));}
  const statuses={draft:'Draft',proposed:'Proposed — not agreed',confirmed:'Confirmed by admin',clarify:required};
  function plainText(data){
    const lines=['SB EMPIRE — BLOCKTEXX RFI RESPONSE','DRAFT · AUD excluding GST · subject to scope and commercial agreement',data.reference.source||'RFI source reference not configured'];
    for(const section of data.sections){lines.push('\n'+section.number+' '+section.title);for(const item of section.items){lines.push('\n'+item.title,item.draft);for(const t of item.tables){lines.push(t.title,t.headers.join(' | '),...t.rows.map(r=>r.join(' | ')));}for(const answer of item.linked){if(answer.status==='answered'&&answer.answer.trim())lines.push('Recorded clarification: '+answer.question+' '+answer.answer);else if(answer.status==='not_applicable')lines.push('Recorded as not applicable: '+answer.question+(answer.answer?' '+answer.answer:''));}if(item.open.length)lines.push(required+': '+item.open.map(q=>q.question).join(' '));if(item.supplement.text)lines.push(statuses[item.supplement.status]+': '+item.supplement.text);}}
    lines.push('\nGrowth scenarios: illustrative demand at the current state mix. Departures and equipment scale proportionally; capacity, routes and depot limits must be validated.');
    for(const s of data.scenarios)lines.push(num(s.tonnes)+'t: proposed '+rate(s.rate)+'; monthly '+money(s.monthly)+' ex GST. Storage separate. '+s.notes);
    return lines.join('\n');
  }
  function render(root,model,reference,onChange){
    model.rfi ||= {responses:{},forecasts:{},scenarios:{}};
    const settings=model.rfi;settings.responses||={};settings.forecasts||={};settings.scenarios||={};
    const data=build(model,reference),openIds=new Set([...root.querySelectorAll('details[open]')].map(x=>x.dataset.rfiId));
    root.replaceChildren();
    root.append(e('p','SB EMPIRE / RESPONSE WORKSPACE','bx-kicker'),e('h2','RFI Response · 11.1–11.8'),e('p','Dynamic draft response · AUD excluding GST · storage separately identified','bx-rfi-subtitle'));
    root.append(e('p','Answers refresh from the current planners, costs, resources, storage and Questions for Blocktexx whenever you open this page. Proposed wording is not an agreed service commitment. Save inputs to keep them between sessions.','bx-muted'));
    const bar=e('div',null,'bx-rfi-controls no-print');
    function button(title,fn,cls='secondary'){const b=e('button',title,cls);b.type='button';b.onclick=fn;bar.append(b);}
    button('Save response',()=>document.getElementById('bx-save').click(),'primary');
    button('Refresh response',()=>render(root,model,reference,onChange));
    button('Expand all',()=>root.querySelectorAll('details.bx-rfi-section').forEach(d=>d.open=true));
    button('Collapse all',()=>root.querySelectorAll('details.bx-rfi-section').forEach(d=>d.open=false));
    button('Copy response',async()=>{try{await navigator.clipboard.writeText(plainText(build(model,reference)));status.textContent='Response copied. Review outstanding clarifications before sharing.';}catch{status.textContent='Copy unavailable in this browser. Use Print response.';}});
    button('Print response',()=>{const closed=[...root.querySelectorAll('details.bx-rfi-section:not([open])')];closed.forEach(d=>d.open=true);document.body.classList.add('bx-print-rfi');window.addEventListener('afterprint',()=>{document.body.classList.remove('bx-print-rfi');closed.forEach(d=>d.open=false);},{once:true});window.print();});
    root.append(bar);const status=e('p','','bx-muted no-print');status.setAttribute('role','status');root.append(status);
    root._rfiObserver?.disconnect();const saveStatus=document.getElementById('bx-save-status');if(saveStatus){status.textContent=saveStatus.textContent;root._rfiObserver=new MutationObserver(()=>status.textContent=saveStatus.textContent);root._rfiObserver.observe(saveStatus,{childList:true,subtree:true,characterData:true});}
    const metrics=e('div',null,'bx-metrics');
    [['RFI historical baseline',num(reference.monthly_kg)+' kg/month'],['Current model intake',num(data.national.kg)+' kg/month'],['Financial model coverage',Object.keys(model.states).join(', ')],['Response items',String(data.sections.reduce((n,s)=>n+s.items.length,0))]].forEach(([label,value])=>{const card=e('div',label);card.append(e('strong',value));metrics.append(card);});root.append(metrics);
    root.append(e('p',reference.error||((reference.source||'RFI reference not configured')+' · '+num(reference.locations)+' scheduled locations · '+num(reference.equipment)+' equipment units. Current calendar intake may differ from the historical reference.'),'bx-muted'));
    root.append(e('p','ACT, TAS and WA require separate scope and pricing confirmation while the operational model covers QLD, NSW, VIC and SA. Unentered amounts are not confirmed zero. Figures labelled model inputs or carrier costs are not approved selling prices.','bx-rfi-alert'));
    const evidence=e('details',null,'bx-rfi-evidence no-print');evidence.append(e('summary','Model completeness · '+data.issues.length+' items to review'));const issues=e('ul');data.issues.forEach(x=>issues.append(e('li',x)));evidence.append(issues);root.append(evidence);
    const costs=e('details',null,'bx-rfi-evidence no-print');costs.append(e('summary','Internal cost evidence · live model, excluded from response export'));drawTable(costs,table('Current monthly cost basis · ex GST',['Cost category','Known amount'],[['Local, decomm and production transport',money(data.national.local)],['Booked interstate freight',money(data.national.interstate)],['Container rental scenario',money(data.national.rental)],['Equipment leases',money(data.national.equipment)],['Known operating total',money(data.national.total)],['Known operating cost per incoming kg',rate(data.national.rate)],['Separate storage contract',money(data.national.storage)]]));costs.append(e('p','Incomplete inputs remain excluded from known subtotals. Changes to operating inputs update this evidence; draft customer selling rates are separately entered.','bx-muted'));root.append(costs);
    for(const section of data.sections){
      const details=e('details',null,'bx-rfi-section');details.dataset.rfiId=section.number;details.open=openIds.has(section.number)||(!root.dataset.initialized&&section.number==='11.1');
      details.append(e('summary',section.number+' '+section.title));
      for(const item of section.items){
        const article=e('article',null,'bx-rfi-item');article.append(e('h3',item.title),e('span','Draft response','bx-rfi-tag'),e('p',item.draft));
        item.tables.forEach(t=>drawTable(article,t));
        if(item.id==='2.1')drawForecastInputs(article);
        if(item.id==='2.3')drawScenarios(article);
        const answered=item.linked.filter(x=>(x.status==='answered'&&x.answer.trim())||x.status==='not_applicable');
        for(const answer of answered){const box=e('div',null,'bx-rfi-answer');box.append(e('strong',answer.status==='not_applicable'?'Recorded as not applicable':'Recorded BlockTexx clarification'),e('p',answer.question),e('p',answer.answer||'No further explanation recorded.'));article.append(box);}
        if(item.open.length){const pending=e('details',null,'bx-rfi-pending');pending.append(e('summary',required+' · '+item.open.length+' linked questions'));const list=e('ul');item.open.forEach(x=>{const li=e('li',x.question);if(x.answer)li.append(e('p','Unconfirmed note: '+x.answer));list.append(li);});pending.append(list);article.append(pending);}
        const supplement=e('div',null,'bx-rfi-supplement');if(item.supplement.text)supplement.append(e('strong',statuses[item.supplement.status]),e('p',item.supplement.text));article.append(supplement);
        const editor=e('details',null,'bx-rfi-editor no-print');editor.dataset.rfiId='note-'+item.id;editor.open=openIds.has(editor.dataset.rfiId);editor.append(e('summary','Add or edit response notes'));const label=e('label','Additional response / agreed terms'),input=e('textarea');input.rows=3;input.maxLength=4000;input.value=item.supplement.text||'';input.setAttribute('aria-label',section.number+' '+item.title+' — response notes');label.append(input);const stateLabel=e('label','Status of additional response'),select=e('select');Object.entries(statuses).forEach(([key,title])=>{const option=e('option',title);option.value=key;select.append(option);});select.value=item.supplement.status||'draft';select.setAttribute('aria-label',section.number+' '+item.title+' — note status');stateLabel.append(select);
        const changed=()=>{settings.responses[item.id]={text:input.value,status:select.value};supplement.replaceChildren();if(input.value.trim())supplement.append(e('strong',statuses[select.value]),e('p',input.value));onChange();};input.oninput=changed;select.onchange=changed;editor.append(label,stateLabel,e('p','Adds to the generated answer without freezing the live figures. Confirmed status applies only to this note.','bx-muted'));article.append(editor);details.append(article);
      }
      root.append(details);
    }
    root.dataset.initialized='true';
    root.append(e('p','Commercial-in-confidence · Draft for review. Changes made on this page are saved with the existing model and revision history.','bx-muted'));
    function field(parent,label,value,{min=0,max=10000000,step='any',change}){const wrap=e('label',label),input=e('input');input.type='number';input.min=String(min);input.max=String(max);input.step=step;input.value=value??'';input.setAttribute('aria-label',label);input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}change(input.value===''?null:Number(input.value));onChange();render(root,model,reference,onChange);};wrap.append(input);parent.append(wrap);}
    function drawForecastInputs(article){const editor=e('details',null,'bx-rfi-editor no-print');editor.dataset.rfiId='forecasts';editor.open=openIds.has('forecasts');editor.append(e('summary','Edit expected monthly ranges'));const grid=e('div',null,'bx-rfi-inputs');for(const s of states){for(const [key,label] of [['low','Minimum'],['high','Maximum']])field(grid,s+' '+label+' kg/month',settings.forecasts[s]?.[key],{change:value=>{settings.forecasts[s]||={};settings.forecasts[s][key]=value;}});}editor.append(grid,e('p','Leave unknown bounds blank. Save validation rejects a minimum above its maximum.'));article.append(editor);}
    function drawScenarios(article){
      article.append(e('p','*Planning estimates: allocate target kg using current model intake where available and the RFI reference for unmodelled states. Scale current departures and two-set equipment quantities proportionally and round up. This does not verify route density, truck payload, staffing, depot/baler throughput, dwell time or contractor availability. Zero-history states need a separate allocation.','bx-muted'));
      for(const s of data.scenarios){const box=e('div',null,'bx-rfi-scenario');box.append(e('h4',num(s.tonnes)+'t/month · proposed commercial response'),e('p','Proposed national selling rate: '+rate(s.rate)+' ex GST. Monthly charge at this target: '+money(s.monthly)+' ex GST. Storage separate. '+(valid(s.rate)?'Draft rate only; inclusions and capacity subject to confirmation.':'No national quote entered.')),e('p',s.notes||required+': describe equipment, depot, collection, baling and linehaul changes, commitments and pricing bands.'));const editor=e('details',null,'bx-rfi-editor no-print');editor.dataset.rfiId='scenario-'+s.key;editor.open=openIds.has(editor.dataset.rfiId);editor.append(e('summary','Edit '+s.key+'t scenario'));const grid=e('div',null,'bx-rfi-inputs');settings.scenarios[s.key]||={tonnes:s.tonnes,rate:null,notes:''};const cfg=settings.scenarios[s.key];if(s.key==='400')field(grid,'Future scenario tonnes/month (400+)',s.tonnes,{min:400,max:10000,change:v=>{cfg.tonnes=v??400;}});field(grid,s.key+'t proposed national rate / kg',s.rate,{max:10000,step:'0.001',change:v=>{cfg.rate=v;}});const label=e('label','Capacity, scope and price changes'),input=e('textarea');input.rows=3;input.maxLength=4000;input.value=s.notes;input.setAttribute('aria-label',s.key+'t capacity and pricing notes');input.onchange=()=>{cfg.notes=input.value;onChange();render(root,model,reference,onChange);};label.append(input);editor.append(grid,label);box.append(editor);article.append(box);}
    }
  }
  return {build,render,plainText,definitions};
})();
