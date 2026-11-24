const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element(extra = {}) {
  return Object.assign({hidden:false, events:{}, attributes:{},
    addEventListener(name, callback) { this.events[name] = callback; },
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    focus() { this.focused = true; }, click() { this.clicked = true; }
  }, extra);
}
const freight = element({dataset:{bfSearch:'BlockTexx freight charges transport rates'}});
const invoice = element({dataset:{bfSearch:'All invoices billing Xero draft'}});
const pods = element({dataset:{bfSearch:'Daily POD proof of delivery'}});
const links = [freight, invoice, pods];
const groups = [[freight, invoice], [pods]].map(items => element({
  querySelector(selector) {
    return selector.startsWith('h2') ? null : items.find(item => !item.hidden);
  }
}));
const nav = element({querySelectorAll(selector) { return selector.startsWith('a') ? links : groups; }});
const wrapper = element({hidden:true});
const input = element({value:'', closest() { return wrapper; }});
const status = element({textContent:''});
const button = element({attributes:{'aria-controls':'bf-office-nav'}});
const desktop = element({matches:false});
const document = {
  querySelector() { return button; },
  getElementById(id) { return {'bf-office-nav':nav,'bf-menu-search':input,'bf-menu-search-status':status}[id]; }
};
vm.runInNewContext(fs.readFileSync('static/binflow-sidebar.js','utf8'), {document, window:{matchMedia:() => desktop}});
const search = value => { input.value=value; input.events.input(); };
const key = name => input.events.keydown({key:name, preventDefault(){}});
assert.equal(wrapper.hidden,false);
assert.equal(nav.hidden,true);
search('  BLOCKTEXX freight  ');
assert.equal(freight.hidden,false);
assert.equal(invoice.hidden,true);
assert.equal(groups[1].hidden,true);
assert.equal(nav.hidden,false);
assert.equal(status.textContent,'1 matching pages');
key('Enter');
assert.equal(freight.clicked,true);
search('proof delivery');
key('ArrowDown');
assert.equal(pods.focused,true);
assert.equal(freight.hidden,true);
search('unknown feature');
assert.ok(links.every(link => link.hidden));
assert.match(status.textContent,/No matching page/);
key('Escape');
assert.equal(input.value,'');
assert.ok(links.every(link => !link.hidden));
assert.ok(groups.every(group => !group.hidden));
assert.equal(status.textContent,'');
console.log('Sidebar search: keywords, mobile expansion, keyboard access, no matches and reset passed.');
