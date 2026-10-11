const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const sandbox = {};
vm.runInNewContext(fs.readFileSync('frontend/speaker-guard.js', 'utf8'), sandbox);
test('speaker feedback is silenced, including tail, but next user turn reaches capture', () => {
  const guard = new sandbox.JarvisSpeakerGuard();
  const frame = () => new Int16Array(320).fill(20000);
  assert.ok(guard.filter(frame(), true, false, 1000).every(x => x === 20000));
  assert.ok(guard.filter(frame(), true, true, 1020).every(x => x === 0));
  guard.ended(2000);
  assert.ok(guard.filter(frame(), true, false, 2400).every(x => x === 0));
  assert.ok(guard.filter(frame(), true, false, 2600).every(x => x === 20000));
});
test('headphone mode preserves barge-in; changing back protects active playback', () => {
  const guard = new sandbox.JarvisSpeakerGuard();
  const frame = () => new Int16Array([5000, -5000]);
  assert.ok(guard.filter(frame(), false, true, 1000).some(x => x !== 0));
  assert.ok(guard.filter(frame(), true, true, 1020).every(x => x === 0));
  assert.ok(guard.filter(frame(), false, false, 1030).some(x => x !== 0));
});
