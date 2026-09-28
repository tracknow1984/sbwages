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
