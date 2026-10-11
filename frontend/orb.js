"use strict";
// Browser-only rendering. No network, model calls, audio capture or stored audio.
globalThis.JarvisOrb = (() => {
  function stateFor(s) {
    if (!s.online) return "offline";
    if (["error", "reconnecting"].includes(s.voice)) return "degraded";
    if (s.voice === "speaking") return "speaking";
    if (s.voice === "hearing") return "hearing";
    if (s.voice === "interrupted") return "interrupted";
    if (s.voice === "muted") return "muted";
    if (s.activity) return "tool";
    if (
      ["thinking", "transcribing", "synthesizing", "connecting"].includes(
        s.voice,
      ) ||
      s.busy
    )
      return "thinking";
    if (s.voice === "listening") return "listening";
    if (!s.ready) return "degraded";
    return s.urgent ? "attention" : "idle";
  }
  function levelFor(s) {
    const value =
      s.voice === "speaking"
        ? s.output
        : ["listening", "hearing", "interrupted"].includes(s.voice)
          ? s.input
          : 0;
    return Number.isFinite(value) ? Math.max(0, Math.min(1, value * 6)) : 0;
  }
  function create(canvas, snapshot, label) {
    const context = canvas.getContext("2d");
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    let visible = false,
      frame = 0,
      previous = 0,
      lastState = "",
      alive = true;
    const stats = {
      frames: 0,
      last_ms: 0,
      peak: 0,
      output_peak: 0,
      state: "idle",
    };
    function draw(now) {
      frame = 0;
      if (!alive || !visible || document.hidden) return;
      const s = snapshot(),
        state = stateFor(s);
      const active = ![
        "idle",
        "offline",
        "degraded",
        "muted",
        "attention",
      ].includes(state);
      const interval = motion.matches ? 250 : active ? 1000 / 30 : 125;
      if (now - previous >= interval || lastState !== state) {
        const began = performance.now();
        previous = now;
        if (lastState !== state) {
          lastState = state;
          canvas.parentElement.dataset.state = state;
          label(state, s);
        }
        const w = 320,
          h = 210;
        const ratio = Math.min(devicePixelRatio || 1, 2);
        if (canvas.width !== w * ratio || canvas.height !== h * ratio) {
          canvas.width = w * ratio;
          canvas.height = h * ratio;
        }
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.clearRect(0, 0, w, h);
        const level = levelFor(s),
          phase = motion.matches ? 0 : now / 1700;
        const color =
          state === "offline"
            ? "99,113,131"
            : ["attention", "degraded"].includes(state)
              ? "221,177,116"
              : "143,210,224";
        const breathing = state === "idle" ? Math.sin(phase) * 1.4 : 0;
        const radius = 49 + breathing + (motion.matches ? 0 : level * 13);
        const glow = context.createRadialGradient(160, 92, 8, 160, 92, 86);
        glow.addColorStop(0, `rgba(${color},.2)`);
        glow.addColorStop(0.65, `rgba(${color},.08)`);
        glow.addColorStop(1, `rgba(${color},0)`);
        context.fillStyle = glow;
        context.fillRect(60, 0, 200, 190);
        // Original spherical lattice and instrument rings. Decorative geometry
        // stays distinct from the real acoustic waveform below.
        const spin = phase * (active ? 0.12 : 0.035);
        const sphere = context.createRadialGradient(
          145,
          76,
          1,
          160,
          92,
          radius + 8,
        );
        sphere.addColorStop(0, `rgba(${color},.17)`);
        sphere.addColorStop(0.72, `rgba(${color},.035)`);
        sphere.addColorStop(1, `rgba(${color},0)`);
        context.fillStyle = sphere;
        context.beginPath();
        context.arc(160, 92, radius + 8, 0, Math.PI * 2);
        context.fill();
        for (let latitude = -3; latitude <= 3; latitude++) {
          const phi = (latitude * Math.PI) / 9;
          const r = Math.cos(phi) * radius;
          context.beginPath();
          for (let j = 0; j <= 48; j++) {
            const theta = (j * Math.PI) / 24 + spin;
            const x = Math.cos(theta) * r;
            const z = Math.sin(theta) * r;
            const y = Math.sin(phi) * radius;
            const px = 160 + x,
              py = 92 + y * 0.87 + z * 0.32;
            if (j) context.lineTo(px, py);
            else context.moveTo(px, py);
          }
          context.strokeStyle = `rgba(${color},.18)`;
          context.lineWidth = 0.55;
          context.stroke();
        }
        for (let meridian = 0; meridian < 12; meridian++) {
          const theta = (meridian * Math.PI) / 6 + spin;
          context.beginPath();
          for (let j = 0; j <= 24; j++) {
            const phi = -Math.PI / 2 + (j * Math.PI) / 24;
            const x = Math.cos(phi) * Math.cos(theta) * radius;
            const z = Math.cos(phi) * Math.sin(theta) * radius;
            const y = Math.sin(phi) * radius;
            if (j) context.lineTo(160 + x, 92 + y * 0.87 + z * 0.32);
            else context.moveTo(160 + x, 92 + y * 0.87 + z * 0.32);
          }
          context.strokeStyle = `rgba(${color},${Math.sin(theta) > 0 ? 0.28 : 0.09})`;
          context.stroke();
        }
        for (let tick = 0; tick < 60; tick++) {
          const angle = (tick * Math.PI) / 30;
          const outer = tick % 5 === 0 ? 86 : 83;
          context.beginPath();
          context.moveTo(160 + Math.cos(angle) * 80, 92 + Math.sin(angle) * 80);
          context.lineTo(
            160 + Math.cos(angle) * outer,
            92 + Math.sin(angle) * outer,
          );
          context.strokeStyle = `rgba(${color},${tick % 5 === 0 ? 0.5 : 0.18})`;
          context.lineWidth = 0.65;
          context.stroke();
        }
        for (let sector = 0; sector < 3; sector++) {
          const angle = (sector * Math.PI * 2) / 3 + spin * 0.5;
          context.beginPath();
          context.arc(160, 92, 74, angle, angle + 1.15);
          context.strokeStyle = `rgba(${color},.55)`;
          context.lineWidth = 1.6;
          context.stroke();
        }
        for (let ring = 0; ring < 3; ring++) {
          context.beginPath();
          context.ellipse(
            160,
            92,
            radius + ring * 9,
            radius * (0.83 + ring * 0.07),
            (state === "thinking" ? phase * 0.14 : 0.3) + ring * 0.75,
            0,
            Math.PI * 2,
          );
          context.strokeStyle = `rgba(${color},${0.75 - ring * 0.23})`;
          context.lineWidth = ring === 0 ? 1.5 : 0.7;
          context.stroke();
        }
        context.fillStyle = `rgba(${color},.9)`;
        context.beginPath();
        context.arc(
          160,
          92,
          3 + (motion.matches ? 0 : level * 2),
          0,
          Math.PI * 2,
        );
        context.fill();
        if (state === "tool") {
          for (let i = 0; i < 3; i++) {
            context.beginPath();
            context.arc(
              160 + Math.cos(phase + i * 2.1) * 74,
              92 + Math.sin(phase + i * 2.1) * 65,
              2,
              0,
              Math.PI * 2,
            );
            context.fill();
          }
        }
        // This line uses real samples; silence is flat, never a decorative waveform.
        const samples = s.samples;
        context.beginPath();
        for (let i = 0; i < 96; i++) {
          const amplitude =
            !motion.matches && level && samples?.length
              ? samples[Math.floor((i * samples.length) / 96)]
              : 0;
          const x = 55 + (i * 210) / 95,
            y = 187 + Math.max(-1, Math.min(1, amplitude || 0)) * 28;
          if (i) context.lineTo(x, y);
          else context.moveTo(x, y);
        }
        context.strokeStyle = `rgba(${color},.7)`;
        context.lineWidth = 1;
        context.stroke();
        stats.frames++;
        stats.last_ms = performance.now() - began;
        stats.peak = Math.max(stats.peak, level);
        if (state === "speaking")
          stats.output_peak = Math.max(stats.output_peak, level);
        stats.state = state;
      }
      frame = requestAnimationFrame(draw);
    }
    function resume() {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      if (alive && visible && !document.hidden)
        frame = requestAnimationFrame(draw);
    }
    const observer = new IntersectionObserver((entries) => {
      visible = entries[0].isIntersecting;
      resume();
    });
    observer.observe(canvas);
    document.addEventListener("visibilitychange", resume);
    return {
      stats,
      destroy() {
        alive = false;
        if (frame) cancelAnimationFrame(frame);
        observer.disconnect();
        document.removeEventListener("visibilitychange", resume);
      },
    };
  }
  return { stateFor, levelFor, create };
})();
