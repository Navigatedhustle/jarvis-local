"use strict";
globalThis.JarvisGraph = (() => {
  const palette = {
    response: "#eff4d5",
    person: "#9bcddb",
    project: "#c6d69d",
    organization: "#c3b1dc",
    event: "#dab682",
    document: "#9eb7df",
    email: "#8ba89c",
    commitment: "#e1a797",
    decision: "#d9c68b",
    memory: "#acb3c4",
  };
  function category(n) {
    return palette[n.type] ? n.type : "memory";
  }
  function layout(nodes, root) {
    const bounded = nodes.slice(0, 75),
      result = new Map();
    const others = bounded
      .filter((n) => n.id !== root)
      .sort(
        (a, b) =>
          category(a).localeCompare(category(b)) || a.id.localeCompare(b.id),
      );
    if (bounded.some((n) => n.id === root))
      result.set(root, { x: 360, y: 280 });
    let offset = 0;
    for (const [capacity, radius] of [
      [12, 105],
      [24, 185],
      [38, 245],
    ]) {
      const ring = others.slice(offset, offset + capacity);
      ring.forEach((n, i) => {
        const angle = (i * Math.PI * 2) / ring.length - Math.PI / 2;
        result.set(n.id, {
          x: 360 + Math.cos(angle) * radius * 1.2,
          y: 280 + Math.sin(angle) * radius,
        });
      });
      offset += capacity;
    }
    return result;
  }
  function filtered(data, types, recent, now = Date.now()) {
    const nodes = data.nodes
      .slice(0, 75)
      .filter(
        (n) =>
          n.id === data.root ||
          (types.has(category(n)) &&
            (!recent ||
              Date.parse(n.activity_at || n.updated_at || "") >=
                now - 30 * 86400000)),
      );
    const ids = new Set(nodes.map((n) => n.id));
    return {
      ...data,
      nodes,
      edges: data.edges
        .filter((e) => ids.has(e.source) && ids.has(e.target))
        .slice(0, 600),
    };
  }
  function svgElement(tag, attrs = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([k, v]) =>
      element.setAttribute(k, String(v)),
    );
    return element;
  }
  function create(data, select) {
    const svg = svgElement("svg", {
      viewBox: "0 0 720 560",
      role: "group",
      "aria-label": "Saved memory relationships",
    });
    svg.classList.add("memory-map");
    const layer = svgElement("g");
    svg.append(layer);
    let scale = 1,
      pan = { x: 0, y: 0 },
      drag = null,
      current = data;
    function transform() {
      layer.setAttribute(
        "transform",
        `translate(${pan.x} ${pan.y}) translate(360 280) scale(${scale}) translate(-360 -280)`,
      );
    }
    function draw(next) {
      current = next;
      layer.replaceChildren();
      const points = layout(next.nodes, next.root);
      next.edges.slice(0, 600).forEach((e) => {
        const a = points.get(e.source),
          b = points.get(e.target);
        if (!a || !b) return;
        const line = svgElement("line", {
          x1: a.x,
          y1: a.y,
          x2: b.x,
          y2: b.y,
          stroke: "#405364",
          "stroke-width": Math.max(
            0.6,
            Math.min(2, (Number(e.weight) || 40) / 60),
          ),
        });
        if (e.status?.includes("candidate"))
          line.setAttribute("stroke-dasharray", "3 4");
        const title = svgElement("title");
        title.textContent = e.relationship + " · " + e.status;
        line.append(title);
        layer.append(line);
      });
      next.nodes.slice(0, 75).forEach((n) => {
        const p = points.get(n.id);
        if (!p) return;
        const group = svgElement("g", {
          transform: `translate(${p.x} ${p.y})`,
          tabindex: "0",
          role: "button",
          "aria-label": `${n.type}: ${n.name}`,
        });
        group.dataset.node = n.id;
        const title = svgElement("title");
        title.textContent = n.name + " · " + n.status;
        const circle = svgElement("circle", {
          r: n.id === next.root ? 18 : 9,
          fill: palette[category(n)],
          stroke: "#101a26",
          "stroke-width": 3,
        });
        const text = svgElement("text", { y: 30, "text-anchor": "middle" });
        text.textContent = n.name.slice(0, 28);
        if (n.id === next.root) group.classList.add("selected");
        group.append(title, circle, text);
        function activate() {
          layer
            .querySelectorAll("g.selected")
            .forEach((g) => g.classList.remove("selected"));
          group.classList.add("selected");
          select(n, current);
        }
        group.addEventListener("click", activate);
        group.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            activate();
          }
        });
        layer.append(group);
      });
      transform();
    }
    svg.addEventListener("pointerdown", (e) => {
      if (!e.target.closest("[data-node]")) {
        drag = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
        svg.setPointerCapture(e.pointerId);
      }
    });
    svg.addEventListener("pointermove", (e) => {
      if (drag) {
        const rect = svg.getBoundingClientRect();
        pan = {
          x: drag.px + ((e.clientX - drag.x) * 720) / rect.width,
          y: drag.py + ((e.clientY - drag.y) * 560) / rect.height,
        };
        transform();
      }
    });
    svg.addEventListener("pointerup", () => {
      drag = null;
    });
    svg.addEventListener("pointercancel", () => {
      drag = null;
    });
    draw(data);
    return {
      element: svg,
      draw,
      zoom(delta) {
        scale = Math.max(0.65, Math.min(2.5, scale + delta));
        transform();
      },
      reset() {
        scale = 1;
        pan = { x: 0, y: 0 };
        transform();
      },
    };
  }
  return { palette, category, layout, filtered, create };
})();
