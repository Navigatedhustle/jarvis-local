"use strict";
globalThis.JarvisBrainNetwork = (() => {
  const labels = {
    person: "People",
    organization: "Organizations",
    project: "Projects",
    document: "Knowledge",
    email: "Email",
    event: "Calendar",
    commitment: "Commitments",
    memory: "Memory",
  };
  async function create({ el, button, api, edit, showDetail }) {
    const box = el("section", undefined, "neural-workspace");
    let disposed = false,
      timer = 0,
      graph = null,
      data = null,
      busy = false,
      pendingRefresh = false,
      focusAfterRefresh = null,
      selected = null,
      type = "";
    box.jarvisDispose = () => {
      disposed = true;
      clearTimeout(timer);
      graph?.dispose();
    };
    const header = el("div", undefined, "neural-heading"),
      metrics = el("div", undefined, "neural-metrics");
    const title = el("div");
    title.append(
      el("span", "JARVIS / KNOWLEDGE SYSTEM", "eyebrow"),
      el("h1", "A mind built around your work."),
      el("p", "Your methods, people and context. Connected."),
    );
    header.append(title, metrics);
    const layout = el("div", undefined, "neural-layout"),
      stage = el("div", undefined, "neural-stage"),
      inspector = el("aside", undefined, "brain-inspector");
    inspector.setAttribute("aria-label", "Selected knowledge");
    const badge = el("span", "SAVED CONTEXT / 3D", "neural-stage-label"),
      refreshStatus = el("span", "Loading saved context…", "neural-sync");
    refreshStatus.setAttribute("role", "status");
    const tools = el("div", undefined, "neural-controls"),
      lenses = el("div", undefined, "neural-lenses");
    lenses.setAttribute("aria-label", "Knowledge categories");
    const search = el("input");
    search.type = "search";
    search.placeholder = "Find a person, topic or memory";
    search.setAttribute("aria-label", "Find a knowledge node");
    const accessible = el("details", undefined, "neural-node-list"),
      summary = el("summary", "Browse saved nodes"),
      nodeList = el("div", undefined, "neural-node-buttons");
    accessible.append(summary, search, nodeList);
    const coverage = el("p", undefined, "neural-coverage");
    function idleInspector() {
      inspector.replaceChildren(
        el("span", "EXPLORE YOUR CONTEXT", "eyebrow"),
        el("h3", "Every point has a source."),
        el(
          "p",
          "Select a node to see what JARVIS knows and how it connects to your work.",
        ),
        button("Add knowledge", () => edit(), "primary"),
      );
      const recent = el("div", undefined, "neural-recent");
      recent.append(el("h4", "Recently updated"));
      [...data.nodes]
        .filter((n) => n.updated_at)
        .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))
        .slice(0, 4)
        .forEach((n) => recent.append(button(n.name, () => select(n))));
      inspector.append(
        recent,
        el(
          "small",
          "Newly visible nodes glow when the map refreshes. Saved item totals grow as context is added.",
        ),
      );
    }
    function select(n) {
      selected = n.id;
      graph.select(n.id);
      inspector.replaceChildren(
        el("span", labels[JarvisBrain3D.group(n)].toUpperCase(), "eyebrow"),
        el("h3", n.name),
        el("span", n.status || n.type, "pill"),
        el(
          "p",
          n.summary ||
            "A saved profile or relationship. Inspect its linked context below.",
        ),
      );
      if (n.metadata?.source_id && n.id.startsWith("record:"))
        inspector.append(
          button("Open source", async () => {
            const r = await api(
              "/record?id=" + encodeURIComponent(n.metadata.source_id),
            );
            showDetail(r.title, r.body);
          }),
        );
      if (n.metadata?.brain_kind)
        inspector.append(
          button("Edit knowledge", () => edit(n.metadata.source_id)),
        );
      const edges = data.edges.filter(
          (e) => e.source === n.id || e.target === n.id,
        ),
        links = el("div", undefined, "neural-connections");
      links.append(el("h4", `${edges.length} visible connections`));
      for (const e of edges.slice(0, 12)) {
        const other = data.nodes.find(
          (v) => v.id === (e.source === n.id ? e.target : e.source),
        );
        if (!other) continue;
        const row = el("div");
        row.append(
          button(other.name, () => select(other)),
          el(
            "small",
            e.relationship.replaceAll("_", " ").toLowerCase() +
              " · " +
              (e.status || "recorded link"),
          ),
        );
        links.append(row);
      }
      if (!edges.length)
        links.append(
          el(
            "small",
            "No recorded link in this view. JARVIS has not invented one.",
          ),
        );
      inspector.append(
        links,
        button("Clear selection", () => {
          selected = null;
          graph.select(null);
          idleInspector();
        }),
      );
    }
    function filter() {
      graph.filter(type, search.value);
      const rows = data.nodes.filter(
        (n) =>
          (!type || JarvisBrain3D.group(n) === type) &&
          n.name.toLowerCase().includes(search.value.toLowerCase()),
      );
      summary.textContent = `Browse ${rows.length} visible nodes`;
      nodeList.replaceChildren(
        ...rows.map((n) => button(n.name, () => select(n))),
      );
      if (!rows.length)
        nodeList.append(
          el(
            "p",
            "No matches in this sampled view. Search All knowledge for saved notes.",
          ),
        );
    }
    function render(next, initial = false) {
      data = next;
      const added = initial ? 0 : graph.update(data);
      const total = data.stats?.total ?? Math.max(0, data.nodes.length - 1);
      metrics.replaceChildren();
      for (const [number, label] of [
        [total, "saved items"],
        [data.edges.length, "visible links"],
        [data.stats?.updated_week || 0, "updated · 7 days"],
      ]) {
        const m = el("div");
        m.append(el("strong", number.toLocaleString()), el("span", label));
        metrics.append(m);
      }
      lenses.replaceChildren();
      const groups = [
        ...new Set(
          data.nodes.filter((n) => n.id !== data.root).map(JarvisBrain3D.group),
        ),
      ];
      for (const key of ["", ...groups]) {
        const control = button(key ? labels[key] : "All context", () => {
          type = key;
          for (const b of lenses.children)
            b.setAttribute("aria-pressed", String(b === control));
          filter();
        });
        control.setAttribute("aria-pressed", String(type === key));
        control.classList.add("neural-lens", key ? "lens-" + key : "lens-all");
        lenses.append(control);
      }
      filter();
      coverage.textContent = `Showing ${Math.max(0, data.nodes.length - 1)} of ${total.toLocaleString()} saved items, plus your owner node. Recent items per category; not your complete mailbox. Lines are recorded links. The wireframe is a visual scaffold, not extra memories.`;
      const current = data.nodes.find((n) => n.id === selected);
      if (current) select(current);
      else {
        selected = null;
        idleInspector();
      }
      refreshStatus.textContent =
        (added ? `${added} newly visible · ` : "") +
        "Updated " +
        new Date(data.as_of).toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
        });
    }
    async function refresh() {
      if (disposed) return;
      if (busy) {
        pendingRefresh = true;
        return;
      }
      busy = true;
      try {
        const next = await api("/brain/graph");
        if (disposed) return;
        if (!next.enabled) throw new Error("Knowledge map is disabled");
        render(next);
        if (focusAfterRefresh) {
          const saved = data.nodes.find((n) => n.id === focusAfterRefresh);
          if (saved) {
            select(saved);
            focusAfterRefresh = null;
          }
        }
      } catch {
        if (!disposed)
          refreshStatus.textContent =
            "Refresh unavailable. Showing the last saved view; retry when connected.";
      } finally {
        busy = false;
        if (pendingRefresh && !disposed) {
          pendingRefresh = false;
          await refresh();
        }
      }
    }
    box.jarvisRefresh = async (id) => {
      focusAfterRefresh = "record:" + id;
      if (graph) await refresh();
    };
    function schedule() {
      timer = setTimeout(async () => {
        if (disposed) return;
        if (box.isConnected && !document.hidden) await refresh();
        if (!disposed) schedule();
      }, 60000);
    }
    box.append(header, lenses, layout, accessible, coverage);
    layout.append(stage, inspector);
    stage.append(badge, refreshStatus);
    try {
      data = await api("/brain/graph");
      if (disposed) return box;
      if (!data.enabled) {
        stage.append(
          el(
            "p",
            "Knowledge map is disabled. Your notes remain available in All knowledge.",
          ),
        );
        return box;
      }
      graph = JarvisBrain3D.create(data, select);
      const pause = button(
        graph.isPaused() ? "Resume orbit" : "Pause orbit",
        () => {
          const stopped = graph.pause(!graph.isPaused());
          pause.textContent = stopped ? "Resume orbit" : "Pause orbit";
        },
      );
      tools.append(
        button("Rotate left", () => graph.rotate(-0.25)),
        button("Rotate right", () => graph.rotate(0.25)),
        button("Zoom in", () => graph.zoom(0.1)),
        button("Zoom out", () => graph.zoom(-0.1)),
        button("Reset view", () => {
          selected = null;
          graph.reset();
          idleInspector();
        }),
        pause,
        button("Refresh map", refresh),
      );
      stage.append(
        graph.element,
        tools,
        el("span", "DRAG TO ORBIT · SELECT TO EXPLORE", "neural-hint"),
      );
      search.oninput = filter;
      render(data, true);
      schedule();
    } catch {
      stage.append(
        el(
          "p",
          "Saved context could not load. Open 3D map to retry; your notes remain available.",
        ),
      );
      refreshStatus.textContent = "Connection needs attention";
    }
    return box;
  }
  return { create };
})();
