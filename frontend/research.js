"use strict";
globalThis.JarvisResearch = (() => {
  function link(source, label) {
    const node = el("a", label || source.title);
    try {
      const url = new URL(source.url);
      if (url.protocol !== "https:" || url.username || url.password)
        return el("span", label || source.title);
      node.href = url.href;
      node.target = "_blank";
      node.rel = "noopener noreferrer";
      node.referrerPolicy = "no-referrer";
    } catch {
      return el("span", label || source.title);
    }
    return node;
  }
  function source(source, number) {
    const row = el("article", undefined, "research-source");
    row.append(
      link(source, `[${number}] ${source.title}`),
      el(
        "small",
        `${source.publisher || "Public source"} · Published ${time(source.at)} · Checked ${time(source.retrieved_at)}`,
      ),
    );
    const details = el("details");
    details.append(
      el(
        "summary",
        source.coverage === "weather_forecast"
          ? "Forecast data"
          : "Retrieved excerpt",
      ),
      el("p", source.excerpt || "Only a title was provided."),
    );
    row.append(details);
    return row;
  }
  async function panel() {
    const box = el("section", undefined, "research-panel record");
    box.setAttribute("aria-label", "AI and market brief");
    let pollCount = 0;
    async function load() {
      let data;
      try {
        data = await api("/research");
      } catch {
        box.replaceChildren(
          el("h2", "AI & market brief"),
          el(
            "p",
            "Research status is unavailable. Your saved work is unaffected.",
          ),
        );
        return;
      }
      box.replaceChildren();
      const busy = ["queued", "running", "pending"].includes(data.job?.status);
      box.append(
        el("small", "OUTSIDE SIGNAL · YOUR NEXT MOVE", "tag"),
        el("h2", "AI & market brief"),
        el(
          "p",
          "Local AI, higher education, research and competitive developments. Selected sources from the last seven days.",
        ),
      );
      const state =
        {
          ready: "Ready",
          degraded: "Partial coverage",
          offline: "Sources unavailable",
          stale: "Needs a refresh",
          configuration_required: "Ready to prepare",
        }[data.status] || data.status;
      const status = el(
        "p",
        `${state} · ${data.built_at ? "Prepared " + time(data.built_at) : "No briefing prepared yet"}${busy ? " · Refresh in progress" : ""}${data.job?.status === "failed" ? " · Last refresh failed; try again" : ""}`,
      );
      status.setAttribute("role", "status");
      box.append(status);
      async function researchAsk(query) {
        if (phoneLayout.matches) await navigate("Voice");
        return ask(query);
      }
      const searchForm = el("form", undefined, "web-search-form");
      const search = el("input");
      search.type = "search";
      search.required = true;
      search.maxLength = 500;
      search.placeholder = "Research a public topic…";
      search.setAttribute("aria-label", "Web research topic");
      const submit = el("button", "Search the web");
      submit.type = "submit";
      searchForm.append(search, submit);
      searchForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const query = search.value.trim();
        if (!query) return;
        submit.disabled = true;
        try {
          await researchAsk("Search the web for " + query);
        } finally {
          submit.disabled = false;
        }
      });
      box.append(
        searchForm,
        el(
          "small",
          "Live public research with source links. Your search text goes to a public search provider; leave private work details out.",
        ),
      );
      const tools = el("div", undefined, "workspace-toolbar");
      const refresh = button(
        busy ? "Preparing briefing…" : "Refresh AI brief",
        async () => {
          refresh.disabled = true;
          try {
            await api("/research/refresh", {});
            pollCount = 0;
            await load();
          } catch {
            refresh.disabled = false;
            toast("Couldn't queue a refresh. Please try again.");
          }
        },
      );
      refresh.disabled = busy;
      tools.append(
        button("Weekday LinkedIn studio", () => navigate("Writing")),
        refresh,
        button("Talk through the brief", () =>
          researchAsk(
            "Give me my AI news briefing and the most useful next step for my HP role.",
          ),
        ),
        button("Weather", () =>
          researchAsk("What is the weather today and tomorrow?"),
        ),
      );
      box.append(tools);
      if (data.reply) {
        const brief = el("details");
        brief.append(
          el("summary", "Read briefing and suggested actions"),
          el("p", data.reply, "research-answer"),
        );
        (data.sources || []).forEach((s, i) => brief.append(source(s, i + 1)));
        box.append(brief);
      }
      box.append(
        el(
          "p",
          `Weather: ${data.weather.city} · ${data.weather.basis === "confirmed_travel_dates" ? "Your confirmed travel dates" : data.weather.basis === "active_calendar_trip" ? "Current travel block in your calendar" : "Home location"}`,
        ),
      );
      const options = el("details");
      options.append(
        el("summary", "Sources, travel and preferences"),
        el(
          "p",
          "Prepared at 06:45 Denver time, Monday–Saturday, or after startup that day. Internal preparation only; this does not send messages or place calls.",
        ),
      );
      const enabled = el("input");
      enabled.type = "checkbox";
      enabled.checked = data.daily_enabled;
      const label = el("label", "Prepare my daily AI brief ");
      label.prepend(enabled);
      enabled.addEventListener("change", async () => {
        enabled.disabled = true;
        try {
          await api("/research/preferences", {
            daily_enabled: enabled.checked,
          });
        } catch {
          enabled.checked = !enabled.checked;
          toast("Preference wasn't saved.");
        } finally {
          enabled.disabled = false;
        }
      });
      options.append(
        label,
        el(
          "p",
          "Public feed scans send no Outlook content. Explicit web searches send your query to a public search provider. Weather sends only the requested city and its public coordinates to the National Weather Service.",
        ),
        el("p", data.weather.note),
      );
      for (const row of data.weather.candidates || []) {
        options.append(
          el(
            "p",
            `${row.city} · ${row.status === "active_calendar_trip" ? "Active calendar trip" : "Travel evidence needs date review"}`,
          ),
          button("Review travel evidence", async () => {
            const r = await api("/record?id=" + encodeURIComponent(row.id));
            showDetail(r.title, r.body || JSON.stringify(r.metadata, null, 2));
          }),
          button("Confirm weather dates", () => {
            showDetail(
              "Weather for your trip",
              `Use ${row.city} on these dates, then return to your home weather. Dates use your Denver calendar. This changes only JARVIS weather preferences.`,
            );
            const form = el("form", undefined, "brain-editor"),
              start = el("input"),
              end = el("input");
            start.type = end.type = "date";
            start.required = end.required = true;
            start.setAttribute("aria-label", "Travel weather start");
            end.setAttribute("aria-label", "Travel weather end");
            const submit = el("button", "Use weather for these dates");
            submit.type = "submit";
            form.append(
              el("label", "First day"),
              start,
              el("label", "Last day"),
              end,
              submit,
            );
            form.addEventListener("submit", async (event) => {
              event.preventDefault();
              submit.disabled = true;
              try {
                await api("/research/travel", {
                  city: row.city,
                  start: start.value,
                  end: end.value,
                  source_id: row.id,
                });
                document.querySelector("#detail").close();
                await load();
              } catch {
                toast(
                  "Check the dates. Trips must be current or upcoming and at most 31 days.",
                );
                submit.disabled = false;
              }
            });
            document.querySelector("#detail-body").append(form);
          }),
        );
      }
      if (data.weather.confirmed_plan) {
        const plan = data.weather.confirmed_plan;
        options.append(
          el(
            "p",
            `Confirmed weather plan: ${plan.city}, ${plan.start} through ${plan.end}.`,
          ),
          button("Clear weather plan", async () => {
            await api("/research/travel/clear", {});
            await load();
          }),
        );
      }
      for (const provider of data.providers || [])
        options.append(
          el(
            "small",
            `${provider.publisher}: ${provider.status.replaceAll("_", " ")}`,
          ),
        );
      box.append(options);
      if (busy && pollCount++ < 60)
        setTimeout(() => {
          if (box.isConnected && current === "Today") load();
        }, 3000);
    }
    await load();
    return box;
  }
  return { panel, source, link };
})();
