class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(320);
    this.offset = 0;
    this.ratio = sampleRate / 16000;
    this.weight = 0;
    this.sum = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (samples) {
      for (let i = 0; i < samples.length; i++) {
        let remaining = 1;
        while (remaining > 1e-8) {
          const take = Math.min(remaining, this.ratio - this.weight);
          this.sum += samples[i] * take;
          this.weight += take;
          remaining -= take;
          if (this.weight >= this.ratio - 1e-8) {
            this.frame[this.offset++] =
              Math.max(-1, Math.min(1, this.sum / this.ratio)) * 32767;
            this.weight = this.sum = 0;
            if (this.offset === this.frame.length) {
              this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
              this.frame = new Int16Array(320);
              this.offset = 0;
            }
          }
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-recorder", PCMRecorder);
