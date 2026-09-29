const assert=require('node:assert/strict');global.window={};
const policy={configured:true,revision:0,administration_pct:7,profit_pct:23,minimum_pct:30,gst_pct:10};
class E{constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.textContent='';this.dataset={};}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;}setAttribute(){}querySelectorAll(){return [];}querySelector(){return null;}get text(){return this.textContent+' '+this.children.map(x=>x.text||'').join(' ');}get publicText(){return this.className?.includes('no-print')?'':this.textContent+' '+this.children.map(x=>x.publicText||'').join(' ');}}
global.document={getElementById:id=>id==='bx-private-pricing'?{textContent:JSON.stringify(policy)}:null,createElement:t=>new E(t)};
for(const file of ['planner','costs','weights','interstate','pricing','national','questions','rfi'])require('../static/blocktexx_'+file+'.js');
const P=window.BlocktexxPricing,N=window.BlocktexxNational,R=window.BlocktexxRFI;
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} != ${b}`);
const model={states:{},sites:[],partners:[],storage:{total_containers:10,free_containers:2,monthly_rate:100,occupied_containers:5,repack_cost:200},interstate:{lanes:{sydney_brisbane_bdouble:{base_trip:1000,fuel_pct:20,tolls_trip:10,other_trip:0}},bookings:[{lane_id:'sydney_brisbane_bdouble',trips:2}]}};
for(const state of ['QLD','NSW','VIC','SA'])model.states[state]={state_code:state,cost_mode:'owned',fixed_monthly:1000,monthly_kg:100,runs:[],resource_pricing:{}};
const a=N.calculate(model),p=P.calculate(a);
close(p.total,a.total*1.3);close(p.administration,a.total*.07);close(p.profit,a.total*.23);close(p.includingGST,p.total*1.1);close(p.rate,p.total/a.kg);close(p.states.reduce((n,s)=>n+s.total,0),p.total);assert.equal(P.calculate(a,{...policy,profit_pct:2}),null);
const before=JSON.stringify(model);const root=new E();N.render(root,model);assert.equal(JSON.stringify(model),before);assert.match(root.publicText,/National Overview · provisional costings/);assert.doesNotMatch(root.publicText,/Profit markup|Administration markup|23.00%|30%|Monthly administration allowance/);assert.match(root.text,/Profit markup/);assert.match(root.text,/Monthly administration allowance/);
const text=R.plainText(R.build(model));assert.match(text,/Provisional state selling rates/);assert.match(text,/provisional selling schedule/);assert.doesNotMatch(text,/administration_pct|profit_pct|Combined markup|Monthly administration allowance|carrier cost inputs/);
model.rfi={scenarios:{'150':{rate:.01}}};const r=R.build(model);close(r.scenarios[0].rate,p.rate);assert.equal(r.scenarios[0].minimumApplied,true);close(r.scenarios[0].monthly,p.rate*150000);
model.storage.monthly_rate=9999;close(P.calculate(N.calculate(model)).total,p.total);
model.states.QLD.fixed_monthly=2000;const changed=P.calculate(N.calculate(model));close(changed.total-p.total,1300);assert.ok(R.build(model).scenarios[0].rate>r.scenarios[0].rate);
for(const s of Object.values(model.states))s.monthly_kg=0;const zero=P.calculate(N.calculate(model));assert.equal(zero.rate,null);close(zero.states.reduce((n,s)=>n+s.total,0)+zero.unallocatedFreight,zero.total);
console.log('Private markup floor, additive pricing, freight reconciliation, GST, dynamic RFI, export privacy and storage separation passed.');
