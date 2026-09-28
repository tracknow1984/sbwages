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
assert.equal(Object.keys(network).length,21);
for(const id of ['net_vic','net_sa','net_wa'])assert.ok(flow.networkEdges.some(edge=>edge[0]===id+'_bale'&&edge[1]==='net_threshold'));
for(const [from,to] of [['net_threshold','net_sydney'],['net_sydney','net_north'],['net_north','net_thread'],['net_qld_bale','net_thread'],['net_hold','net_make']])assert.ok(flow.networkEdges.some(edge=>edge[0]===from&&edge[1]===to));
assert.equal(flow.positions({net_vic_decomm:{x:30,y:240},net_hold:{x:620,y:1040}},'network').net_hold.y,1040);
console.log('Partner-state consolidation via Sydney and direct QLD route passed.');

for(const state of ['vic','sa','wa','nsw','qld']){assert.ok(flow.networkEdges.some(e=>e[0]==='net_'+state&&e[1]==='net_'+state+'_decomm'));assert.ok(flow.networkEdges.some(e=>e[0]==='net_'+state+'_decomm'&&e[1]==='net_'+state+'_bale'));}
