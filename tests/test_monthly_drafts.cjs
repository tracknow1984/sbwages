const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');const source=fs.readFileSync('static/rent_monthly.js','utf8');
function boot({storage=new Map(),revision='r0',areas={}}={}){
 const els={},timers=new Map(),requests=[];let tid=0;
 const make=()=>({children:[],dataset:{},events:{},textContent:'',hidden:false,append(...x){this.children.push(...x)},replaceChildren(){this.children=[]},addEventListener(k,f){this.events[k]=f},setAttribute(){}});
 const rows=Array.from({length:48},(_,i)=>{const row=make();row.dataset.month=String(i+1);const field=(kind,value)=>({name:`month_${kind}_${i+1}`,value,validity:{valid:true},setAttribute(){},closest(){return row},matches(){return true}});row.a=field('area',String(areas[i+1]||0));row.r=field('rate','15');const outputs={};row.querySelector=s=>s==='.monthly-area'?row.a:s==='.monthly-rate'?row.r:outputs[s]??=make();return row});
 const details=Array.from({length:4},(_,i)=>({open:i===0}));const inputs=rows.flatMap(r=>[r.a,r.r]);
 const form=els['monthly-form']=make();form.dataset={user:'7',revision};form.action='/save';form.querySelectorAll=s=>s==='.monthly-row'?rows:s==='.monthly-year'?details:inputs;
 const document={getElementById:id=>els[id]??=make(),createElement:make};
 const ctx={document,Intl,Number,Math,Array,Object,JSON,String,Error,AbortSignal:{timeout:()=>undefined},window:{addEventListener(){}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setTimeout(fn){timers.set(++tid,fn);return tid},clearTimeout(id){timers.delete(id)},FormData:class{constructor(){this.values=Object.fromEntries(inputs.map(el=>[el.name,el.value]))}set(k,v){this.values[k]=v}},fetch(url,options){return new Promise((resolve,reject)=>requests.push({values:options.body.values,resolve:revision=>resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({ok:true,revision})}),reject}))}};
 vm.runInNewContext(source,ctx);
 return {rows,els,storage,requests,timers,edit(month,value){rows[month-1].a.value=value;form.events.input({target:rows[month-1].a})},fire(){const [id,fn]=Array.from(timers.entries()).at(-1);timers.delete(id);return fn()}};
}
(async()=>{
 let app=boot();const summary=app.els['monthly-year-summary-1'].children[0];
 app.edit(5,'');assert.equal(app.els['monthly-year-summary-1'].children[0],summary);assert.equal(app.rows[4].a.value,'');assert.equal(app.timers.size,0);
 let restored=boot({storage:app.storage});assert.equal(restored.rows[4].a.value,'');
 app.edit(5,'1000');let saving=app.fire();assert.equal(app.requests[0].values.month_area_5,'1000');app.edit(6,'2000');app.requests[0].resolve('r1');await saving;
 assert.equal(app.rows[5].a.value,'2000');saving=app.fire();assert.equal(app.requests[1].values.month_area_6,'2000');assert.equal(app.requests[1].values.revision,'r1');app.requests[1].resolve('r2');await saving;assert.equal(app.storage.size,0);
 app.edit(48,'3000');saving=app.fire();app.requests[2].reject(new Error('Offline'));await saving;assert(app.storage.size>0);
 restored=boot({storage:app.storage,revision:'r2',areas:{5:1000,6:2000}});assert.equal(restored.rows[47].a.value,'3000');
 const conflict=boot({storage:app.storage,revision:'newer',areas:{1:100}});assert.equal(conflict.rows[47].a.value,'0');assert.equal(conflict.els['restore-monthly-draft'].hidden,false);
 app.edit(5,'40000');assert.equal(app.rows[47].a.value,'3000');assert.equal(app.timers.size,0);
 console.log('PASS: Month 5 blank draft recovery, stable summary nodes, queued in-flight edits, Month 48 offline recovery, revision conflict and over-area retention');
})().catch(e=>{console.error(e);process.exit(1)});
