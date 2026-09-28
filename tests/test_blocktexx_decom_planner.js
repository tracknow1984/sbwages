const assert=require('node:assert/strict');
class Element {
 constructor(tag,text=''){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};this.style={};this.events={};this.classList={add(){},remove(){},toggle(){}};}
 append(...nodes){this.children.push(...nodes);}
 prepend(...nodes){this.children.unshift(...nodes);}
 setAttribute(key,val){this[key]=val;}
 addEventListener(key,fn){this.events[key]=fn;}
 querySelectorAll(){return [];}
 querySelector(){return null;}
 get text(){return this.textContent+' '+this.children.map(n=>n.text??n.textContent??'').join(' ');}
}
global.document={createElement:tag=>new Element(tag),body:new Element('body'),getElementById:()=>null};
global.window={BlocktexxCosts:{renderMode(){},dailySummary(){return{};},renderOperator(){},kgText(){return '';},summaryRateText(){return '';}}};
require('../static/blocktexx_planner.js');
const planner=window.createBlocktexxPlanner();
const make=(id,type,minutes,slots=[])=>({id,name:id,activity_type:type,site_ids:[],runs_4w:1,planner_slots:slots,drive_min:minutes,service_min:0,depot_min:0,prep_min:0,wait_min:0,break_min:0});
const local=make('Local customer collection','collection',400,[{week:1,day:0}]);
const decom=make('Partner delivery','deliver_decomm',160);
const collected=make('Partner return','collect_decomm',20,[{week:1,day:1}]);
const stored=make('Storage return','return_storage',30);
const states=Object.fromEntries(['QLD','NSW','VIC','SA'].map(s=>[s,{runs:[local,decom,collected,stored]}]));
const model={states,sites:[]};
for(const state of Object.keys(states)){
 let root=new Element('section');planner.render(root,model,state,null,()=>{},()=>{},null,'decom');
 assert.ok(root.text.includes(state+' · Decom Planner'));
 assert.ok(root.text.includes('Partner delivery'));
 assert.ok(root.text.includes('Partner return'));
 assert.ok(!root.text.includes('Local customer collection'));
 assert.ok(!root.text.includes('Storage return'));
 assert.ok(root.text.includes('2h 20m available for decom'));
 assert.ok(root.text.includes('8h 40m available for decom'));
 root=new Element('section');planner.render(root,model,state,null,()=>{},()=>{},null,'local');
 assert.ok(root.text.includes('Local customer collection'));
 assert.ok(!root.text.includes('Storage return'));
 root=new Element('section');planner.render(root,model,state,null,()=>{},()=>{},null,'production');
 assert.ok(root.text.includes(state+' · Production Planner'));
 assert.ok(root.text.includes('Storage return'));
 assert.ok(root.text.includes('2h 20m available for production'));
 assert.ok(!root.text.includes('Local customer collection'));
 assert.ok(!root.text.includes('Partner delivery'));
 assert.ok(!root.text.includes('Partner return'));
}
assert.match(planner.allocationError(states.QLD.runs,decom,[{week:1,day:0}]),/OVER/);
const visible=window.BlocktexxPlannerScope.runs(states.QLD.runs,'decom');visible[0].planner_slots=[{week:1,day:2}];
assert.equal(model.states.QLD.runs[1].planner_slots[0].day,2);
assert.equal(model.states.QLD.runs.length,4);
console.log('Decom/Local planner rendering passed across all four states; shared hours and single movement records preserved.');

for(const state of Object.keys(states)){
 const empty={states:{[state]:{runs:[local,stored]}},sites:[]};
 const root=new Element('section');planner.render(root,empty,state,null,()=>{},()=>{},null,'decom');
 assert.ok(root.text.includes('No decom movements added for '+state));
 assert.ok(root.text.includes('Week 4'));
 for(const forbidden of ['Local customer collection','Storage return','financial analysis','kg'])assert.ok(!root.text.includes(forbidden),forbidden);
 assert.ok(root.text.includes('available for decom'));
 assert.ok(root.text.includes('Local, decom and production time already booked'));
 assert.equal(empty.states[state].runs.length,2);
}
console.log('Empty decom calendars show no local data, hours or finances in every state.');
const shared=[make('local hours','collection',240,[{week:1,day:0}]),make('decomm hours','deliver_decomm',120,[{week:1,day:0}]),make('production hours','deliver_threadtexx',60,[{week:1,day:0}])];
let combined=planner.dayLoad(shared,1,0);
assert.equal(combined.minutes,420);
assert.equal(combined.byPlanner.local.minutes,240);
assert.equal(combined.byPlanner.decom.minutes,120);
assert.equal(combined.byPlanner.production.minutes,60);
shared[2].planner_slots=[{week:1,day:5}];
assert.equal(planner.dayLoad(shared,1,0).minutes,360);
assert.equal(planner.dayLoad(shared,1,5).minutes,60);
for(const scope of ['local','decom','production']){
 const root=new Element('section');planner.render(root,{states:{NSW:{runs:shared}},sites:[]},'NSW',null,()=>{},()=>{},null,scope);
 assert.match(root.text,/Saturday/);
 assert.match(root.text,/Local: 4h/);
 assert.match(root.text,/Decomm: 2h/);
 assert.match(root.text,/Production: 1h/);
}
shared[1].wait_min=null;
combined=planner.dayLoad(shared,1,0);
assert.equal(combined.unknown,true);assert.equal(combined.byPlanner.decom.unknown,true);
assert.match(planner.allocationError(shared,shared[2],[{week:1,day:0}]),/incomplete/);
console.log('All planner hours, reallocation, cross-planner weekends and incomplete-time protection passed.');
