"use strict";
globalThis.JarvisBrain3D = (() => {
  const LIMIT = 300;
  const palette = {
    person: "#6fe5f3",
    organization: "#6fe5f3",
    project: "#b7a1ff",
    document: "#ffcf86",
    email: "#50cbb0",
    event: "#739fff",
    commitment: "#ff939f",
    memory: "#d0b4ff",
  };
  const group = (n) => (palette[n.type] ? n.type : "memory");
  function hash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++)
      h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    // Avalanche the salted hash; nearby salts must not form correlated bands.
    h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
    h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
    return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
  }
  // Two lobes form a spatial scaffold, not invented knowledge or relationships.
  function shell(u, v, radius = 1) {
    const side = u < Math.PI ? 1 : -1;
    return {
      x: side * 0.16 + Math.sin(v) * Math.sin(u) * radius * 0.91,
      y: Math.cos(v) * radius * 0.79,
      z: Math.sin(v) * Math.cos(u) * radius * 0.83,
    };
  }
  function positions(nodes, root) {
    return new Map(
      nodes.slice(0, LIMIT).map((n) => {
        const u = hash(n.id + ":u") * Math.PI * 2;
        const v = Math.acos(2 * hash(n.id + ":v") - 1);
        return [
          n.id,
          n.id === root
            ? { x: 0, y: 0, z: 0 }
            : shell(u, v, 0.68 + hash(n.id + ":r") * 0.32),
        ];
      }),
    );
  }
  function project(p, yaw, pitch, zoom = 1) {
    const x = p.x * Math.cos(yaw) - p.z * Math.sin(yaw);
    const z0 = p.x * Math.sin(yaw) + p.z * Math.cos(yaw);
    const y = p.y * Math.cos(pitch) - z0 * Math.sin(pitch);
    const z = p.y * Math.sin(pitch) + z0 * Math.cos(pitch);
    const scale = (310 * zoom) / (1.45 - z * 0.26);
    return {
      x: 500 + x * scale * 1.2,
      y: 310 + y * scale,
      z,
      size: 2.3 + (z + 1) * 1.65,
    };
  }
  function changes(before, after) {
    const old = new Set(before.nodes.map((n) => n.id));
    return after.nodes.filter((n) => !old.has(n.id)).map((n) => n.id);
  }
  function create(initial, select) {
    let data = initial,
      points = positions(data.nodes, data.root);
    const canvas = document.createElement("canvas");
    canvas.width = 1600;
    canvas.height = 1024;
    canvas.className = "brain-canvas";
    canvas.setAttribute(
      "aria-label",
      "3D knowledge network. Drag to orbit, or use the controls and searchable node list.",
    );
    canvas.setAttribute("role", "img");
    const ctx = canvas.getContext("2d");
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    let yaw = 0.32,
      pitch = -0.13,
      scale = 1.1,
      drag = null,
      projected = [];
    let selected = null,
      hovered = null,
      lens = "",
      query = "",
      added = new Set(),
      addedAt = 0;
    let paused = reduced.matches,
      visible = false,
      disposed = false,
      frame = 0,
      last = 0;
    let connected = new Set();
    function active(n) {
      return (
        (!lens || group(n) === lens) &&
        (!query || n.name.toLowerCase().includes(query))
      );
    }
    function neighbors() {
      connected = new Set(selected ? [selected] : []);
      for (const e of data.edges)
        if (e.source === selected || e.target === selected) {
          connected.add(e.source);
          connected.add(e.target);
        }
    }
    function path(points) {
      ctx.beginPath();
      points.forEach((p, i) =>
        i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y),
      );
      ctx.stroke();
    }
    function draw(now = performance.now()) {
      ctx.setTransform(1.6, 0, 0, 1.6, 0, 0);
      ctx.clearRect(0, 0, 1000, 640);
      const bg = ctx.createRadialGradient(500, 310, 15, 500, 310, 460);
      bg.addColorStop(0, "#12313e");
      bg.addColorStop(0.52, "#0b1c2a");
      bg.addColorStop(1, "#060d17");
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, 1000, 640);
      // Quiet coordinate rings distinguish the decorative scaffold from evidence edges.
      ctx.lineWidth = 0.7;
      ctx.strokeStyle = "rgba(100,178,199,.12)";
      for (const r of [250, 280]) {
        ctx.beginPath();
        ctx.ellipse(500, 310, r * 1.35, r, 0, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.strokeStyle = "rgba(95,190,214,.12)";
      for (let j = 1; j < 15; j++) {
        const line = [];
        for (let i = 0; i <= 70; i++)
          line.push(
            project(
              shell((i / 70) * Math.PI * 2, (j / 15) * Math.PI),
              yaw,
              pitch,
              scale,
            ),
          );
        path(line);
      }
      for (let j = 0; j < 22; j++) {
        const line = [];
        for (let i = 1; i < 40; i++)
          line.push(
            project(
              shell((j / 22) * Math.PI * 2, (i / 40) * Math.PI),
              yaw,
              pitch,
              scale,
            ),
          );
        path(line);
      }
      const mapped = new Map(
        [...points].map(([id, p]) => [id, project(p, yaw, pitch, scale)]),
      );
      const byId = new Map(data.nodes.map((n) => [n.id, n]));
      for (const e of data.edges.slice(0, 940)) {
        const a = mapped.get(e.source),
          b = mapped.get(e.target);
        if (!a || !b) continue;
        const focused = e.source === selected || e.target === selected;
        const eligible =
          active(byId.get(e.source)) && active(byId.get(e.target));
        ctx.globalAlpha = selected
          ? focused
            ? 0.85
            : 0.065
          : eligible
            ? 0.29
            : 0.025;
        ctx.strokeStyle = focused
          ? "#aeedf3"
          : palette[group(byId.get(e.source))] || "#66b4ca";
        ctx.lineWidth = focused ? 1.6 : 0.7;
        ctx.setLineDash(e.status?.startsWith("candidate") ? [3, 5] : []);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.quadraticCurveTo(
          (a.x + b.x) / 2 + (a.z - b.z) * 24,
          (a.y + b.y) / 2 - 20,
          b.x,
          b.y,
        );
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      projected = data.nodes
        .slice(0, LIMIT)
        .map((n) => ({ ...mapped.get(n.id), node: n }))
        .sort((a, b) => a.z - b.z);
      for (const p of projected) {
        const id = p.node.id,
          root = id === data.root;
        const focused = id === selected || id === hovered;
        const bright = active(p.node) && (!selected || connected.has(id));
        ctx.globalAlpha = bright ? 0.65 + (p.z + 1) * 0.15 : 0.12;
        const color = palette[group(p.node)],
          radius = root ? 9 : p.size;
        const glow = ctx.createRadialGradient(
          p.x,
          p.y,
          0,
          p.x,
          p.y,
          radius * 4,
        );
        glow.addColorStop(0, color + "85");
        glow.addColorStop(1, color + "00");
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = focused || root ? "#e6fbff" : color;
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
        ctx.fill();
        if (focused || root || (added.has(id) && now - addedAt < 9000)) {
          ctx.strokeStyle = color;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(p.x, p.y, radius + 6, 0, Math.PI * 2);
          ctx.stroke();
        }
        if (focused || root) {
          ctx.globalAlpha = 1;
          ctx.font = "500 12px Segoe UI";
          const label = root ? "DEMO" : p.node.name.slice(0, 46);
          const width = ctx.measureText(label).width;
          const lx = Math.max(8, Math.min(990 - width, p.x + 16));
          ctx.fillStyle = "#07111de8";
          ctx.fillRect(lx - 5, p.y - 10, width + 10, 22);
          ctx.fillStyle = "#e1f5ff";
          ctx.fillText(label, lx, p.y + 5);
        }
      }
      ctx.globalAlpha = 1;
    }
    function tick(now) {
      frame = 0;
      if (disposed || !visible || document.hidden || paused) return;
      if (now - last > 42) {
        if (!drag && !selected && !hovered)
          yaw += Math.min(now - last, 60) * 0.000045;
        draw(now);
        last = now;
      }
      frame = requestAnimationFrame(tick);
    }
    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      if (!disposed && visible && !document.hidden && !paused) {
        last = performance.now();
        frame = requestAnimationFrame(tick);
      }
    }
    const observer = new IntersectionObserver((entries) => {
      visible = entries[0].isIntersecting;
      sync();
    });
    observer.observe(canvas);
    const motion = () => {
      if (reduced.matches) paused = true;
      sync();
      draw();
    };
    document.addEventListener("visibilitychange", sync);
    reduced.addEventListener("change", motion);
    function hit(e) {
      const r = canvas.getBoundingClientRect(),
        x = ((e.clientX - r.left) * 1000) / r.width,
        y = ((e.clientY - r.top) * 640) / r.height;
      return [...projected]
        .reverse()
        .find(
          (p) => active(p.node) && Math.hypot(x - p.x, y - p.y) < p.size + 9,
        );
    }
    canvas.onpointerdown = (e) => {
      drag = { x: e.clientX, y: e.clientY, distance: 0 };
      canvas.setPointerCapture(e.pointerId);
    };
    canvas.onpointermove = (e) => {
      if (!drag) {
        hovered = hit(e)?.node.id || null;
        canvas.style.cursor = hovered ? "pointer" : "grab";
        draw();
        return;
      }
      const dx = e.clientX - drag.x,
        dy = e.clientY - drag.y;
      drag.distance += Math.abs(dx) + Math.abs(dy);
      yaw += dx * 0.006;
      pitch = Math.max(-1, Math.min(1, pitch + dy * 0.006));
      drag.x = e.clientX;
      drag.y = e.clientY;
      draw();
    };
    canvas.onpointerup = (e) => {
      if (drag && drag.distance < 5) {
        const p = hit(e);
        if (p) {
          selected = p.node.id;
          neighbors();
          select(p.node);
          draw();
        }
      }
      drag = null;
    };
    canvas.onpointercancel = () => {
      drag = null;
    };
    canvas.onpointerleave = () => {
      hovered = null;
      draw();
    };
    draw();
    return {
      element: canvas,
      update(next) {
        added = new Set(changes(data, next));
        addedAt = performance.now();
        data = next;
        points = positions(data.nodes, data.root);
        if (!points.has(selected)) selected = null;
        neighbors();
        draw();
        return added.size;
      },
      select(id) {
        selected = id;
        neighbors();
        draw();
      },
      filter(type, text = "") {
        lens = type;
        query = text.toLowerCase();
        draw();
      },
      rotate(delta) {
        yaw += delta;
        draw();
      },
      zoom(delta) {
        scale = Math.max(0.65, Math.min(1.65, scale + delta));
        draw();
      },
      reset() {
        yaw = 0.32;
        pitch = -0.13;
        scale = 1.1;
        selected = null;
        neighbors();
        draw();
      },
      pause(value) {
        paused = value || reduced.matches;
        sync();
        return paused;
      },
      isPaused() {
        return paused;
      },
      dispose() {
        disposed = true;
        cancelAnimationFrame(frame);
        observer.disconnect();
        document.removeEventListener("visibilitychange", sync);
        reduced.removeEventListener("change", motion);
      },
    };
  }
  return { LIMIT, palette, group, positions, project, changes, create };
})();
