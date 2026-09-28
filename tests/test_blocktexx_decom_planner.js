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
 root=new Element('section');planner.render(root,model,state,null,()=>{},()=>{},null,'local');
 assert.ok(root.text.includes('Local customer collection'));
 assert.ok(root.text.includes('Storage return'));
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
 for(const forbidden of ['Local customer collection','Storage return','financial analysis','spare','allocated','kg'])assert.ok(!root.text.includes(forbidden),forbidden);
 assert.equal(empty.states[state].runs.length,2);
}
console.log('Empty decom calendars show no local data, hours or finances in every state.');
