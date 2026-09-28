const assert=require('node:assert/strict');global.window={};
for(const file of ['planner','costs','weights','interstate','national'])require('../static/blocktexx_'+file+'.js');
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} != ${b}`);
const p={enabled:true,contractor_basis:'hourly',staff_qty:1,staff_hourly:10,paid_hours_week:10,workers_comp_pct:10,super_pct:10,truck_insurance_month:100,truck_lease_month:200,fuel_month:300,owned_other_month:0,building_insurance_month:0,building_lease_month:100,contractor_hourly:50,contractor_daily:100,minimum_hours:4,free_wait_minutes:0,demurrage_hourly:50,contractor_other_month:0};
const run=(id,type='collection',kg=100)=>({id,name:id,site_ids:[],activity_type:type,runs_4w:1,planner_slots:[{week:1,day:0,pickup_kg:kg}],drive_min:60,service_min:0,depot_min:0,prep_min:0,wait_min:0,break_min:0});
const model={states:{},sites:[],interstate:{lanes:{sydney_brisbane_bdouble:{base_trip:1000,fuel_pct:20,tolls_trip:10,other_trip:0}},bookings:[{lane_id:'sydney_brisbane_bdouble',trips:2,kg_trip:999999}]},storage:{total_containers:10,free_containers:2,monthly_rate:100,occupied_containers:5,repack_cost:200}};
for(const state of ['QLD','NSW','VIC','SA']){model.states[state]={state_code:state,cost_mode:'owned',cost_profile:{...p},runs:[run(state),run(state+'decom','deliver_decomm',99999)],monthly_kg:99999,resource_pricing:{bin120:{purchase_each:100,weekly_rent_each:2}}};model.sites.push({state,id:state,containers:{bin120:1}});}
let a=window.BlocktexxNational.calculate(model);
close(a.kg,400*13/12);close(a.storage,800);close(a.interstate,2420*13/12);close(a.rental,4*2*2*52/12);close(a.purchase,800);close(a.repack,1000);
close(a.local,4*(100*52/12*1.2+600)+200);close(a.total,a.local+a.interstate+a.rental);close(a.rate,a.total/a.kg);close(a.total,a.lines.reduce((n,r)=>n+(r.value||0),0));
assert.equal(a.issues.length,0);
model.states.NSW.cost_mode='mixed';model.states.NSW.day_operators={'1:0':'contractor'};
a=window.BlocktexxNational.calculate(model);close(a.states.find(s=>s.state==='NSW').local,window.BlocktexxCosts.periodSummary(model.states.NSW,null,model.sites).cost*13/12);
model.states.QLD.runs[0].planner_slots[0].pickup_kg=0;
a=window.BlocktexxNational.calculate(model);assert.equal(a.states[0].kg,0);close(a.kg,300*13/12);
model.states.QLD.cost_profile.truck_lease_month=null;model.interstate.lanes.sydney_brisbane_bdouble.other_trip=null;
a=window.BlocktexxNational.calculate(model);assert.ok(a.issues.some(s=>s.includes('Truck lease')));assert.ok(a.issues.some(s=>s.includes('Freight other')));assert.ok(a.rate>0);
model.interstate.bookings=[];a=window.BlocktexxNational.calculate(model);assert.equal(a.interstate,0);
model.states.SA.runs=[];model.states.SA.monthly_kg=500;a=window.BlocktexxNational.calculate(model);assert.equal(a.states.find(s=>s.state==='SA').kg,500);
// The renderer handles incomplete inputs without hiding the known cost/kg.
class E{constructor(){this.children=[];this.textContent='';}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;}get text(){return this.textContent+' '+this.children.map(x=>x.text||'').join(' ');}}
global.document={createElement:()=>new E()};const root=new E();window.BlocktexxNational.render(root,model);assert.match(root.text,/National Overview/);assert.match(root.text,/provisional/);assert.match(root.text,/Monthly cost breakdown/);
console.log('National monthly totals, weighted kg rate, mixed operators, zero overrides, partial prices, one-off separation and rendering passed.');
const beforeBaler=window.BlocktexxNational.calculate(model);
model.states.NSW.resource_equipment={baler:{quantity:1,purchase_each:25000,monthly_lease_each:900}};
const withBaler=window.BlocktexxNational.calculate(model);
close(withBaler.total-beforeBaler.total,900);close(withBaler.equipment,900);
close(withBaler.transport,beforeBaler.transport);close(withBaler.purchase,beforeBaler.purchase);
close(withBaler.total,withBaler.lines.reduce((n,r)=>n+(r.value||0),0));
model.states.NSW.resource_equipment.baler.quantity=0;
close(window.BlocktexxNational.calculate(model).equipment,0);
console.log('Equipment lease counted once monthly; purchase value excluded from charged costs.');

const operating=window.BlocktexxNational.calculate(model);
model.storage.monthly_rate=9999;
model.storage.repack_cost=7777;
const changedStorage=window.BlocktexxNational.calculate(model);
close(changedStorage.total,operating.total);close(changedStorage.rate,operating.rate);
assert.notEqual(changedStorage.storage,operating.storage);
assert.equal(changedStorage.lines.some(r=>r.category==='Storage contract'),false);
model.storage={};
const missingStorage=window.BlocktexxNational.calculate(model);
close(missingStorage.total,operating.total);assert.deepEqual(missingStorage.issues,operating.issues);
console.log('Storage prices and missing storage inputs do not affect operating totals or cost/kg.');
