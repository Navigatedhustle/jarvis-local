const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
for (const rate of [16000, 44100, 48000]) {
  test(`device audio at ${rate} Hz becomes fifty 20ms 16kHz PCM frames`, () => {
    const frames = [];
    let Processor;
    const sandbox = { sampleRate: rate, AudioWorkletProcessor: class { constructor() { this.port = { postMessage: b => frames.push(new Int16Array(b)) }; } }, registerProcessor: (_, p) => { Processor = p; } };
    vm.runInNewContext(fs.readFileSync('frontend/voice-worklet.js', 'utf8'), sandbox);
    const recorder = new Processor();
    for (let i = 0; i < rate; i += 128) recorder.process([[new Float32Array(Math.min(128, rate - i)).fill(0.5)]]);
    assert.equal(frames.length, 50);
    for (const frame of frames) { assert.equal(frame.length, 320); assert.ok(frame.every(x => Math.abs(x - 16383) <= 1)); }
  });
}
