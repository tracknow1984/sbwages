window.BlocktexxActivityLabels={
 collection:'Customer collection',deliver_decomm:'Deliver to decomm partner',collect_decomm:'Collect from decomm partner',
 deliver_threadtexx:'Deliver to Threadtexx',deliver_blocktexx:'Deliver to Blocktexx',return_storage:'Return to storage'
};
window.createBlocktexxMovements=function(getModel,getState,onChange){
 const root=document.getElementById('bx-movements');
 let editing=null;
 const e=(t,v)=>{const n=document.createElement(t);if(v!=null)n.textContent=v;return n;};
 function render(){
  const model=getModel(),state=getState(),data=model.states[state];
  const existing=editing?data.runs.find(r=>r.id===editing):null;
  root.replaceChildren();
  const details=e('details');details.open=!!existing;details.className='bx-card';
  details.append(e('summary',existing?'Edit local movement':'Add decomm / Threadtexx / Blocktexx / storage movement'));
  const form=e('form'),grid=e('div');grid.className='bx-settings';const inputs={};
  function field(key,label,value,type='text',options){
   const l=e('label',label),input=e(options?'select':'input');
   if(options)options.forEach(([v,t])=>{const o=e('option',t);o.value=v;input.append(o);});
   else {input.type=type;if(type==='number'){input.min=0;input.max=key==='movement_kg'?1000000:10080;input.step='any';}}
   input.value=value??'';input.setAttribute('aria-label',label);inputs[key]=input;l.append(input);grid.append(l);return input;
  }
  field('activity_type','Movement type',existing?.activity_type||'deliver_decomm','text',Object.entries(window.BlocktexxActivityLabels).filter(([k])=>k!=='collection'));
  field('partner_id','Decomm partner',existing?.partner_id||'','text',[['','Select partner'],...model.partners.filter(p=>p.state===state).map(p=>[p.id,p.name])]);
  field('name','Movement name',existing?.name||'');
  field('origin','From / origin address',existing?.origin||data.depot).required=true;
  field('destination','To / destination address',existing?.destination||'').required=true;
  field('cargo','What is being moved',existing?.cargo||'');
  field('movement_kg','Movement weight kg (not new intake)',existing?.movement_kg,'number');
  field('km','Movement kilometres',existing?.km,'number');
  for(const [key,label] of [['drive_min','Driving minutes'],['service_min','Loading / unloading minutes'],['depot_min','Depot handling minutes'],['prep_min','Preparation minutes'],['wait_min','Waiting minutes'],['break_min','Break minutes']])
   field(key,label,existing?existing[key]:(key==='drive_min'?null:0),'number');
  field('notes','Movement notes',existing?.notes||'');
  function defaults(){
   const type=inputs.activity_type.value,partner=model.partners.find(p=>p.id===inputs.partner_id.value);
   inputs.partner_id.disabled=!['deliver_decomm','collect_decomm'].includes(type);
   inputs.partner_id.required=!inputs.partner_id.disabled;
   if(inputs.partner_id.disabled)inputs.partner_id.value='';
   inputs.name.value=window.BlocktexxActivityLabels[type];
   inputs.origin.value=type==='collect_decomm'?(partner?.address||''):type==='return_storage'?'':data.depot;
   inputs.destination.value=type==='deliver_decomm'?(partner?.address||''):['collect_decomm','return_storage'].includes(type)?data.depot:'';
  }
  inputs.activity_type.onchange=defaults;inputs.partner_id.onchange=defaults;
  if(!existing)defaults();else {inputs.partner_id.disabled=!['deliver_decomm','collect_decomm'].includes(existing.activity_type);inputs.partner_id.required=!inputs.partner_id.disabled;}
  form.append(grid,e('p','Enter the actual origin, destination and cargo. Include each leg only once: collection runs may already include a depot return. Downstream cargo requires a separate load-capacity check; customer switch-out counts are not reused.'));
  const error=e('p');error.className='bx-warning';error.setAttribute('role','status');
  const save=e('button',existing?'Update movement draft':'Add movement to Needs allocation');save.type='submit';save.className='primary';
  form.onsubmit=event=>{
   event.preventDefault();if(!form.reportValidity())return;
   const r=existing||{id:crypto.randomUUID(),site_ids:[],runs_4w:null,planner_slots:[],included_loads:1,original_sequence:'',evidence:''};
   for(const [key,input] of Object.entries(inputs))r[key]=input.type==='number'?(input.value===''?null:Number(input.value)):input.value.trim();
   r.name=r.name||window.BlocktexxActivityLabels[r.activity_type];r.status='estimated';r.sequence=r.origin+' → '+r.destination;
   r.evidence='User-entered downstream movement; verify route times, cargo capacity and addresses.';
   if(!existing)data.runs.push(r);
   editing=null;onChange();render();
  };
  form.append(error,save);
  if(existing){
   const cancel=e('button','Cancel');cancel.type='button';cancel.onclick=()=>{editing=null;render();};
   const remove=e('button','Remove movement');remove.type='button';remove.onclick=()=>{if(!confirm('Remove this movement and its calendar allocations?'))return;data.runs=data.runs.filter(r=>r.id!==existing.id);editing=null;onChange();render();};
   form.append(cancel,remove);
  }
  details.append(form);root.append(details);
 }
 return {render,edit:r=>{editing=r.id;render();root.scrollIntoView({block:'start',behavior:'smooth'});},reset:()=>{editing=null;render();}};
};
