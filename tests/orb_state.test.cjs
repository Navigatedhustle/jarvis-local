const { test } = require("node:test");
const assert = require("node:assert/strict");
require("../frontend/orb.js");
const { stateFor, levelFor } = globalThis.JarvisOrb;
const base = { online: true, ready: true, voice: "off" };
test("orb reflects actual activity and connection rather than invented thinking", () => {
  assert.equal(stateFor(base), "idle");
  assert.equal(stateFor({ ...base, online: false, voice: "speaking" }), "offline");
  assert.equal(stateFor({ ...base, ready: false }), "degraded");
  assert.equal(stateFor({ ...base, busy: true }), "thinking");
  assert.equal(stateFor({ ...base, urgent: true }), "attention");
  for (const voice of ["listening", "hearing", "speaking", "muted", "interrupted"])
    assert.equal(stateFor({ ...base, voice }), voice);
  assert.equal(stateFor({ ...base, activity: "Searching saved Outlook" }), "tool");
  assert.equal(stateFor({ ...base, voice: "speaking", activity: "Searching saved Outlook" }), "speaking");
});
test("silent, muted and idle states have no fake acoustic energy", () => {
  for (const voice of ["off", "muted", "thinking"])
    assert.equal(levelFor({ voice, input: .1, output: .1 }), 0);
  assert.equal(levelFor({ voice: "listening", input: 0 }), 0);
  assert.ok(Math.abs(levelFor({ voice: "hearing", input: .05 }) - .3) < 1e-9);
  assert.ok(Math.abs(levelFor({ voice: "speaking", input: 1, output: .1 }) - .6) < 1e-9);
  assert.equal(levelFor({ voice: "speaking", output: Infinity }), 0);
  assert.equal(levelFor({ voice: "listening", input: -1 }), 0);
  assert.equal(levelFor({ voice: "speaking", output: 1 }), 1);
});
