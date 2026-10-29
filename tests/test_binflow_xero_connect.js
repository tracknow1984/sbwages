const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/binflow-xero-connect.js'), 'utf8');

function run(href, failNavigation = false) {
  const destinations = [];
  const context = {
    URL,
    document: {getElementById: id => id === 'xero-authorize' && href ? {href} : null},
    window: {location: {replace: url => {
      if (failNavigation) throw new Error('Navigation unavailable');
      destinations.push(url);
    }}},
  };
  vm.runInNewContext(source, context);
  return destinations;
}

const expected = 'https://login.xero.com/identity/connect/authorize?response_type=code&state=test-state';
assert.deepEqual(run(expected), [expected]);
for (const href of [null, 'invalid', 'https://untrusted.example/',
  'https://login.xero.com/other', 'https://user:password@login.xero.com/identity/connect/authorize']) {
  assert.deepEqual(run(href), []);
}
assert.deepEqual(run(expected, true), []);
console.log('Xero navigation: valid handoff, rejected destinations and fallback passed.');
