const {test} = require("node:test");
const assert = require("node:assert/strict");
require("../frontend/graph-view.js");
const graph=globalThis.JarvisGraph;
test("graph layout is bounded, deterministic and does not mutate evidence",()=>{
  const nodes=Array.from({length:100},(_,i)=>({id:String(i),type:i%2?'person':'project'}));
  const before=JSON.stringify(nodes),a=graph.layout(nodes,'0'),b=graph.layout(nodes,'0');
  assert.equal(a.size,75);assert.deepEqual(a,b);assert.equal(JSON.stringify(nodes),before);
  assert.deepEqual(a.get('0'),{x:360,y:280});
  for(const p of a.values())assert.ok(Number.isFinite(p.x)&&p.x>=0&&p.x<=720&&p.y>=0&&p.y<=560);
});
test("category/date filtering removes edges rather than inventing replacement relationships",()=>{
  const now=Date.parse('2026-09-20T00:00:00Z');
  const data={root:'0',nodes:[{id:'0',type:'person'},{id:'1',type:'project',activity_at:'2026-09-19'},{id:'2',type:'email',activity_at:'2020-01-01'},{id:'3',type:'project'}],edges:[{source:'0',target:'1'},{source:'1',target:'2'},{source:'0',target:'3'}]};
  const view=graph.filtered(data,new Set(['project','email']),true,now);
  assert.deepEqual(view.nodes.map(n=>n.id),['0','1']);
  assert.deepEqual(view.edges,[{source:'0',target:'1'}]);
  assert.equal(graph.filtered(data,new Set(),false,now).nodes.length,1);
});
