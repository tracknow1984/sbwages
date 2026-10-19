'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const inputs=new Map();let submit;const label={textContent:''};
const form={elements:{namedItem:name=>inputs.get(name)},append:input=>inputs.set(input.name,input),addEventListener:(name,fn)=>{if(name==='submit')submit=fn;}};
class Clock extends Date {constructor(...args){super(...(args.length?args:[Clock.current]));}}
Clock.current='2026-10-18T21:00:00Z';
vm.runInNewContext(fs.readFileSync('static/binflow-clock.js','utf8'),{
  Date:Clock,Intl:{DateTimeFormat:()=>({resolvedOptions:()=>({timeZone:'Australia/Brisbane'})})},
  document:{querySelectorAll:selector=>selector==='[data-device-now]'?[label]:[form],createElement:()=>({})}
});
assert.match(label.textContent,/Australia\/Brisbane/);
Clock.current='2026-10-18T22:15:12Z';submit();
assert.equal(inputs.get('device_at').value,'2026-10-18T22:15:12.000Z');
assert.equal(inputs.get('device_zone').value,'Australia/Brisbane');
Clock.current='2026-10-19T06:45:12Z';submit();
assert.equal(inputs.get('device_at').value,'2026-10-19T06:45:12.000Z');
assert.equal(inputs.size,2);
console.log('Phone clock records the button-tap time and reuses its fields.');
