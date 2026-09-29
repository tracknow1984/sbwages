const assert=require('node:assert/strict');global.window={};
for(const file of ['planner','costs','weights','interstate','national','questions','rfi'])require('../static/blocktexx_'+file+'.js');
const R=window.BlocktexxRFI;
const model={states:{},sites:[],partners:[],storage:{total_containers:10,free_containers:2,monthly_rate:100,occupied_containers:5,repack_cost:200},interstate:{lanes:{sydney_brisbane_bdouble:{base_trip:1000,fuel_pct:20,tolls_trip:10,other_trip:0}},bookings:[{lane_id:'sydney_brisbane_bdouble',trips:2}]}};
for(const state of ['QLD','NSW','VIC','SA']){model.states[state]={state_code:state,cost_mode:'owned',fixed_monthly:1000,selling_per_kg:2,monthly_kg:100,runs:[],resource_pricing:{bin120:{purchase_each:100,weekly_rent_each:2}}};model.sites.push({state,id:state,containers:{bin120:1}});}
const ref={source:'Synthetic RFI',monthly_kg:420,states:{ACT:{kg:10},WA:{kg:10},TAS:{kg:0}}};
let a=R.build(model,ref);assert.equal(a.sections.length,8);assert.equal(a.sections.flatMap(x=>x.items).length,58);assert.equal(a.forecasts.find(x=>x.state==='WA').kg,null);assert.equal(a.mixTotal,420);assert.equal(a.scenarios[2].tonnes,400);assert.equal(a.scenarios[0].monthly,null);assert.equal(a.national.kg,400);
const text=R.plainText(a);assert.match(text,/11.8 Contract terms/);assert.match(text,/BlockTexx clarification required/);assert.ok(!text.includes('Known operating total'));
model.states.QLD.selling_per_kg=2.3;model.states.QLD.monthly_kg=200;model.storage.monthly_rate=200;
model.interstate.lanes.sydney_brisbane_bdouble.fuel_pct=25;
model.rfi={responses:{'1.1':{text:'Additional agreed scope',status:'confirmed'}},scenarios:{'400':{tonnes:500,rate:2,notes:'Extra trucks'}},forecasts:{WA:{low:0,high:80}}};
model.clarification_answers={q010:{status:'answered',answer:'Net kg at first receipt'},q081:{status:'awaiting',answer:'Not yet agreed'}};
const b=R.build(model,ref);assert.ok(Math.abs(b.national.total-a.national.total-100*13/12)<1e-6);assert.equal(b.national.storage,1600);assert.equal(b.scenarios[2].monthly,1000000);assert.equal(b.forecasts.find(x=>x.state==='WA').high,80);assert.match(R.plainText(b),/\$2.300\/kg/);assert.match(R.plainText(b),/Net kg at first receipt/);assert.ok(b.sections[0].items[0].open.some(q=>q.id==='q081'));assert.ok(!b.sections[0].items[0].open.some(q=>q.id==='q010'));
model.states.QLD.fixed_monthly=2000;const c=R.build(model,ref);assert.equal(c.national.total,b.national.total+1000);assert.equal(c.national.storage,b.national.storage);
// Minimal DOM exercises the complete renderer, including notes, filters and editable forecasts.
class Element{constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.textContent='';}append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;}setAttribute(){}querySelectorAll(){return [];}get text(){return this.textContent+' '+this.children.map(x=>x.text||'').join(' ');}}
global.document={createElement:tag=>new Element(tag),getElementById:()=>null};const root=new Element('section');R.render(root,model,ref,()=>{});assert.match(root.text,/11.8 Contract terms/);assert.match(root.text,/Internal cost evidence/);assert.match(root.text,/500t\/month/);
assert.deepEqual(R.definitions.map(s=>s[1].length),[10,4,7,6,7,9,7,8]);
console.log('RFI: all 58 bullets, live price/volume/fuel/storage changes, scenarios, clarification status and renderer passed.');
