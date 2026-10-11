"use strict";
// Explicit speaker turn-taking, not an acoustic echo cancellation claim.
globalThis.JarvisSpeakerGuard = class {
  constructor() {
    this.until = 0;
  }
  ended(now) {
    this.until = now + 600;
  }
  filter(samples, enabled, playing, now) {
    if (playing) this.ended(now);
    if (enabled && (playing || now < this.until)) samples.fill(0);
    return samples;
  }
};
