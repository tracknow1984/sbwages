const assert=require('node:assert/strict');
global.window={};
require('../static/blocktexx_planner.js');require('../static/blocktexx_costs.js');
const C=window.BlocktexxCosts;
const p={enabled:true,contractor_basis:'hourly',staff_qty:0,staff_hourly:0,paid_hours_week:0,workers_comp_pct:0,super_pct:0,truck_insurance_month:0,truck_lease_month:0,fuel_month:0,owned_other_month:0,building_insurance_month:0,building_lease_month:3640/12,contractor_hourly:100,contractor_daily:700,minimum_hours:4,free_wait_minutes:0,demurrage_hourly:100,contractor_other_month:0};
const run=(id,kg,day=0,type='collection')=>({id,activity_type:type,runs_4w:1,planner_slots:[{week:1,day,pickup_kg:kg}],drive_min:60,service_min:0,depot_min:0,prep_min:0,wait_min:0,break_min:0});
const data={cost_profile:p,cost_mode:'contractor',runs:[run('a',100),run('b',300),run('transfer',900,0,'deliver_threadtexx'),run('c',100,1)]};
let d=C.dailySummary(data,1,0);assert.equal(d.kg,400);assert.equal(d.cost,410);assert.equal(d.rate,410/400);
let w=C.periodSummary(data,1);assert.equal(w.kg,500);assert.equal(w.cost,870);assert.equal(w.rate,870/500);assert.equal(C.dailySummary(data,1,2).cost,10);assert.equal(C.dailySummary(data,1,2).rate,null);
assert.ok(Math.abs(C.periodCosts(data,1).totals[1]-w.cost)<1e-8);
assert.ok(Math.abs(C.periodCosts(data,null).totals[1]-C.periodSummary(data).cost)<1e-8);
data.runs[0].planner_slots[0].pickup_kg=null;assert.equal(C.dailySummary(data,1,0).rate,410/300);assert.equal(C.dailySummary(data,1,0).provisional,true);assert.equal(C.periodSummary(data,1).rate,870/400);
data.runs[0].planner_slots[0].pickup_kg=0;assert.equal(C.dailySummary(data,1,0).kg,300);
data.cost_profile.enabled=false;assert.equal(C.dailySummary(data,1,0).cost,null);
data.cost_profile.enabled=true;data.cost_profile.contractor_hourly=null;
d=C.dailySummary(data,1,0);assert.equal(d.cost,10);assert.equal(d.rate,10/300);assert.equal(d.complete,false);assert.equal(d.provisional,true);assert.deepEqual(d.missingCosts,['Contractor base charge']);assert.match(C.summaryRateText(d),/provisional/);
assert.equal(C.summaryRateText(C.dailySummary(data,1,2)),'No pickup kg');
assert.equal(C.periodSummary(data,1).cost,70);assert.equal(C.periodSummary(data,1).rate,70/400);
data.cost_profile.contractor_hourly=100;data.cost_profile.building_insurance_month=null;
d=C.dailySummary(data,1,0);assert.equal(d.cost,410);assert.equal(d.rate,410/300);assert.deepEqual(d.missingCosts,['Building insurance']);
data.cost_profile.building_insurance_month=0;data.cost_profile.building_lease_month=0;data.cost_profile.contractor_hourly=0;
d=C.dailySummary(data,1,0);assert.equal(d.rate,0);assert.equal(d.provisional,false);
data.runs[0].drive_min=null;assert.equal(C.dailySummary(data,1,0).provisional,true);assert.equal(C.dailySummary(data,1,0).rate,0);
console.log('Daily kg: shared minimum, transfer exclusion, overheads, weighted rates, missing/zero weights and incomplete prices passed.');
require('../static/blocktexx_weights.js');
const W=window.BlocktexxWeights,model={sites:[{id:'s1'},{id:'s2'}],weight_history:{pickups:[{site_id:'s1',kg:300},{site_id:'s1',kg:500},{site_id:'s2',kg:100},{site_id:'',kg:999}]}};
W.sync(model);assert.equal(model.sites[0].sample_pickup_kg,400);
const sr={site_ids:['s1','s2']};assert.equal(W.pickup(sr,{},model.sites).kg,500);
assert.equal(W.pickup(sr,{pickup_by_site:{s1:0}},model.sites).kg,100);
model.sites[0].scenario_pickup_kg=1000;assert.equal(W.pickup(sr,{},model.sites).kg,1100);
assert.equal(W.pickup(sr,{pickup_kg:50},model.sites).kg,50);
assert.equal(W.pickup({activity_type:'collect_decomm'}, {pickup_kg:999}, model.sites).kg,0);
assert.equal(W.pickup({site_ids:['missing']},{},model.sites).missing,1);
const be=C.breakEven({selling_per_kg:.5},{complete:true,missing:0,cost:1000,kg:1500});assert.equal(be.target,2000);assert.equal(be.result,-250);
assert.equal(C.breakEven({selling_per_kg:0},{complete:true,missing:0,cost:1000,kg:0}).result,-1000);
assert.equal(C.breakEven({selling_per_kg:.5},{complete:false,missing:0,cost:1000,kg:1500}).result,null);
console.log('Historical averages, per-customer and per-occurrence scenarios, zero overrides and break-even arithmetic passed.');

// Building overheads apply only to NSW and VIC, including on quiet days.
for(const state_code of ['QLD','SA','NSW','VIC']) {
  const d={state_code,cost_mode:'owned',runs:[],cost_profile:{...p,enabled:true,staff_qty:0,truck_insurance_month:0,truck_lease_month:0,fuel_month:0,owned_other_month:0,building_insurance_month:null,building_lease_month:3640/12}};
  const excluded=['QLD','SA'].includes(state_code);
  assert.equal(C.calculate(d).shared,excluded?0:null);
  const day=C.dailySummary(d,1,0);
  assert.equal(day.cost,excluded?0:10);
  assert.deepEqual(day.missingCosts,excluded?[]:['Building insurance']);
  assert.equal(C.periodCosts(d,1).rows.some(row=>row[0].startsWith('Building')), !excluded);
}
console.log('State building exclusions passed for QLD/SA and retained for NSW/VIC.');
// Mixed operations: committed company costs remain; contractor days are billed once.
const mixedProfile={...p,enabled:true,staff_qty:1,staff_hourly:10,paid_hours_week:7,workers_comp_pct:0,super_pct:0,truck_insurance_month:0,truck_lease_month:0,fuel_month:260,owned_other_month:0,building_insurance_month:0,building_lease_month:0,contractor_hourly:100,contractor_daily:700,minimum_hours:4,free_wait_minutes:0,demurrage_hourly:100,contractor_other_month:0};
const mixed={state_code:'QLD',cost_mode:'mixed',cost_profile:mixedProfile,day_operators:{'1:0':'owned','1:1':'contractor'},runs:[run('own',100,0),run('sub-a',200,1),run('sub-b',200,1)]};
let mix=C.periodSummary(mixed,1);
assert.equal(mix.kg,500);assert.equal(mix.cost,590);assert.equal(mix.rate,1.18);
assert.equal(mix.components.owned,190);assert.equal(mix.components.contractor,400);assert.equal(mix.operators.owned.kg,100);assert.equal(mix.operators.contractor.kg,400);
assert.equal(C.dailySummary(mixed,1,0).cost,130);assert.equal(C.dailySummary(mixed,1,1).cost,410);assert.equal(C.dailySummary(mixed,1,2).cost,10);
assert.ok(Math.abs(C.calculate(mixed).selected-C.periodSummary(mixed).cost*13/12)<1e-8);
mixed.cost_profile.contractor_basis='daily';assert.equal(C.periodSummary(mixed,1).components.contractor,700);
mixed.runs[1].wait_min=60;assert.equal(C.periodSummary(mixed,1).components.contractor,800);
mixed.cost_profile.contractor_basis='hourly';mixed.runs[1].wait_min=0;
delete mixed.day_operators['1:1'];mix=C.periodSummary(mixed,1);assert.equal(mix.operators.unassigned.days,1);assert.equal(mix.provisional,true);assert.equal(C.calculate(mixed).selected,null);
mixed.day_operators['1:1']='contractor';mixed.cost_profile.contractor_hourly=null;mix=C.periodSummary(mixed,1);assert.equal(mix.cost,190);assert.equal(mix.provisional,true);assert.ok(mix.missingCosts.includes('Contractor base charge'));
mixed.cost_profile.contractor_hourly=100;mixed.state_code='NSW';mixed.cost_profile.building_lease_month=3640/12;mix=C.periodSummary(mixed,1);assert.equal(mix.components.shared,70);assert.equal(mix.cost,660);
// Assignments persist when modes change, and pure modes ignore them.
mixed.cost_mode='contractor';assert.equal(C.periodSummary(mixed,1).operators.contractor.kg,500);assert.equal(C.periodSummary(mixed,1).components.owned,0);
mixed.cost_mode='owned';assert.equal(C.periodSummary(mixed,1).operators.owned.kg,500);assert.equal(C.periodSummary(mixed,1).components.contractor,0);
console.log('Mixed day allocations, weighted cost/kg, fixed overheads, shared minimums, daily pricing and missing costs passed.');
