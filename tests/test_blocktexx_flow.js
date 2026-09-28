const assert=require('node:assert/strict');global.window={};require('../static/blocktexx_flow.js');
const flow=window.BlocktexxFlow,layout=flow.positions();
assert.equal(Object.keys(layout).length,11);
assert.equal(flow.edges.filter(edge=>edge[0]==='decision').length,2);
assert.ok(flow.edges.some(edge=>edge[0]==='storage'&&edge[1]==='production'));
const edge=flow.edges[0],before=flow.geometry(edge,layout).d;
layout.dispatch.x+=40;assert.notEqual(flow.geometry(edge,layout).d,before);
const moved=flow.positions({dispatch:{x:-100,y:2000}});
assert.equal(moved.dispatch.x,8);assert.equal(moved.dispatch.y,660);
assert.equal(flow.positions({dispatch:{x:100,y:200}}).dispatch.x,100);
for(const edge of flow.edges){const path=flow.geometry(edge,layout);assert.ok(!path.d.includes('NaN'));assert.ok(layout[edge[0]]&&layout[edge[1]]);}
console.log('Flow branches, retained positions, drag boundaries and attached connectors passed.');
const network=flow.positions({},'network');
assert.equal(Object.keys(network).length,11);
for(const id of ['net_vic','net_sa','net_wa'])assert.ok(flow.networkEdges.some(edge=>edge[0]===id&&edge[1]==='net_threshold'));
for(const [from,to] of [['net_threshold','net_sydney'],['net_sydney','net_north'],['net_north','net_thread'],['net_qld','net_thread'],['net_hold','net_make']])assert.ok(flow.networkEdges.some(edge=>edge[0]===from&&edge[1]===to));
assert.equal(flow.positions({net_hold:{x:475,y:740}},'network').net_hold.y,740);
console.log('Partner-state consolidation via Sydney and direct QLD route passed.');
