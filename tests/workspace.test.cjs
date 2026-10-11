const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../frontend/brain-3d.js');
require('../frontend/workspace.js');
test('3D geometry is bounded, deterministic and rotates depth without mutating evidence',()=>{
  const nodes=Array.from({length:400},(_,i)=>({id:String(i)}));
  const before=JSON.stringify(nodes), positions=JarvisBrain3D.positions(nodes,'0');
  assert.equal(positions.size,300);assert.deepEqual(positions,JarvisBrain3D.positions(nodes,'0'));
  const p=positions.get('1'), a=JarvisBrain3D.project(p,0,0),b=JarvisBrain3D.project(p,1,1);
  assert.notDeepEqual(a,b);assert.ok([a.x,a.y,a.z,b.x,b.y,b.z].every(Number.isFinite));
  assert.equal(JSON.stringify(nodes),before);
});
test('calendar date navigation crosses months and DST without drifting days',()=>{
  assert.equal(JarvisWorkspace.addDay('2026-12-31',1),'2027-01-01');
  assert.equal(JarvisWorkspace.addDay('2026-11-01',1),'2026-11-02');
  assert.equal(JarvisWorkspace.dateKey(new Date('2026-09-20T05:00:00Z')),'2026-09-19');
});
test('legacy incomplete approvals are rendered safely and cannot appear ready to approve',()=>{
  assert.equal(JarvisWorkspace.reviewable({kind:'calendar.create',payload:{subject:'Legacy'}}),false);
  assert.equal(JarvisWorkspace.reviewable({kind:'email.send',payload:{}}),false);
  assert.equal(JarvisWorkspace.reviewable({kind:'calendar.create',payload:{subject:'Review',start:{dateTime:'2027-01-01T10:00:00Z'},end:{dateTime:'2027-01-01T11:00:00Z'},attendees:[]}}),true);
});

test('appending knowledge preserves existing spatial positions and highlights only new IDs',()=>{
  const a={nodes:[{id:'owner'},{id:'note-a'},{id:'person-b'}]};
  const b={nodes:[...a.nodes,{id:'note-c'}]};
  const first=JarvisBrain3D.positions(a.nodes,'owner'),next=JarvisBrain3D.positions(b.nodes,'owner');
  for(const [id,p] of first) assert.deepEqual(next.get(id),p);
  assert.deepEqual(JarvisBrain3D.changes(a,b),['note-c']);
  assert.deepEqual(JarvisBrain3D.changes(b,a),[]);
  assert.deepEqual(JarvisBrain3D.changes(a,{nodes:a.nodes.map(n=>({...n,name:'Edited'}))}),[]);
  const reordered=JarvisBrain3D.positions([...b.nodes].reverse(),'owner');
  for(const [id,p] of next) assert.deepEqual(reordered.get(id),p);
});
