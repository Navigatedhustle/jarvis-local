"use strict";
globalThis.JarvisWorkspace = (() => {
  const kinds = {
    sop: "SOP",
    framework: "Framework",
    working_note: "Working note",
    reference: "Reference",
  };
  function helpers() {
    return { el, button, api, showDetail, navigate };
  }
  async function brain() {
    const { el, button, api } = helpers(),
      box = el("section");
    const tabs = el("div", undefined, "workspace-tabs"),
      content = el("div");
    let epoch = 0,
      network = null,
      mode = "map";
    box.jarvisDispose = () => {
      ++epoch;
      network?.jarvisDispose();
    };
    async function notes(kind = "") {
      mode = "notes";
      network?.jarvisDispose();
      network = null;
      const request = ++epoch;
      const rows = await api("/brain/notes?kind=" + encodeURIComponent(kind));
      if (request !== epoch) return;
      content.replaceChildren();
      const tools = el("div", undefined, "workspace-toolbar"),
        search = el("input");
      search.type = "search";
      search.placeholder = "Search your SOPs and notes";
      search.setAttribute("aria-label", "Search second brain");
      const list = el("div", undefined, "brain-note-list");
      const draw = (rows) => {
        list.replaceChildren();
        for (const row of rows) {
          const card = el("article", undefined, "record");
          card.append(
            el("small", kinds[row.metadata.brain_kind]),
            el("h3", row.title),
            el("p", row.body.slice(0, 180)),
            button("Open note", () => edit(row.id)),
          );
          list.append(card);
        }
        if (!rows.length)
          list.append(
            empty(
              "Start with a method you use.",
              "Add an SOP, a framework or a working note. JARVIS can retrieve it when you ask.",
            ),
          );
      };
      tools.append(
        search,
        button("Search notes", async () =>
          draw(
            await api(
              "/brain/notes?q=" +
                encodeURIComponent(search.value) +
                "&kind=" +
                encodeURIComponent(kind),
            ),
          ),
        ),
      );
      const categories = el("div", undefined, "workspace-tabs");
      for (const [value, label] of Object.entries(kinds))
        categories.append(button(label, () => notes(value)));
      content.append(categories, tools, list);
      draw(rows);
    }
    async function edit(id) {
      const row = id
        ? await api("/brain/note?id=" + encodeURIComponent(id))
        : null;
      const dialog = document.querySelector("#detail"),
        body = document.querySelector("#detail-body");
      document.querySelector("#detail-title").textContent = row
        ? "Edit knowledge"
        : "Add to your second brain";
      dialog.classList.remove("graph-dialog");
      body.replaceChildren();
      const form = el("form", undefined, "brain-editor"),
        title = el("input"),
        kind = el("select"),
        text = el("textarea");
      title.required = true;
      title.maxLength = 200;
      title.value = row?.title || "";
      title.setAttribute("aria-label", "Note title");
      title.placeholder = "Give it a clear name";
      kind.setAttribute("aria-label", "Note type");
      for (const [value, name] of Object.entries(kinds)) {
        const option = el("option", name);
        option.value = value;
        kind.append(option);
      }
      kind.value = row?.metadata.brain_kind || "sop";
      text.required = true;
      text.maxLength = 50000;
      text.rows = 16;
      text.value = row?.body || "";
      text.setAttribute("aria-label", "Note content");
      text.placeholder =
        "Purpose\n\nWhen to use this\n\nSteps\n\nWhat good looks like";
      const save = el("button", "Save knowledge", "primary"),
        status = el("p");
      status.setAttribute("role", "status");
      const importLabel = el("label", "Import Markdown or plain text"),
        importFile = el("input");
      importFile.type = "file";
      importFile.accept = ".md,.txt,text/plain,text/markdown";
      importFile.setAttribute("aria-label", "Import knowledge file");
      importFile.onchange = async () => {
        const file = importFile.files?.[0];
        if (!file) return;
        if (file.size > 200000 || !/\.(md|txt)$/i.test(file.name)) {
          status.textContent = "Choose a Markdown or text file up to 200 KB.";
          return;
        }
        const content = await file.text();
        if (content.length > 50000) {
          status.textContent =
            "Keep a note under 50,000 characters. Split longer documents into useful sections.";
          return;
        }
        text.value = content;
        if (!title.value)
          title.value = file.name.replace(/\.(md|txt)$/i, "").slice(0, 200);
        status.textContent = "Loaded for review. Save knowledge to keep it.";
      };
      importLabel.append(importFile);
      form.append(
        title,
        kind,
        importLabel,
        text,
        el(
          "small",
          "Private Markdown on Nano, indexed for JARVIS. Content never changes approval permissions.",
        ),
        save,
        status,
      );
      if (row) {
        const download = el("a", "Download Markdown");
        download.href =
          "/api/brain/note?download=true&id=" + encodeURIComponent(row.id);
        download.download = "jarvis-note.md";
        form.append(download);
      }
      form.onsubmit = async (e) => {
        e.preventDefault();
        save.disabled = true;
        try {
          const saved = await api("/brain/notes", {
            id: row?.id,
            revision: row?.revision,
            title: title.value,
            body: text.value,
            kind: kind.value,
          });
          dialog.close();
          if (mode === "map" && network?.jarvisRefresh)
            await network.jarvisRefresh(saved.id);
          else await notes();
          toast("Knowledge saved. JARVIS can find it now.");
        } catch (error) {
          status.textContent = error.message;
        } finally {
          save.disabled = false;
        }
      };
      body.append(form);
      dialog.showModal();
    }
    async function map() {
      mode = "map";
      network?.jarvisDispose();
      const request = ++epoch;
      content.replaceChildren(el("p", "Connecting saved context…"));
      const next = await JarvisBrainNetwork.create({
        el,
        button,
        api,
        edit,
        showDetail,
      });
      if (request !== epoch) {
        next.jarvisDispose();
        return;
      }
      network = next;
      content.replaceChildren(next);
    }
    tabs.append(
      button("3D map", map),
      button("New note", () => edit(), "primary"),
    );
    const conversation = button("Show conversation", () => {
      if (globalThis.JarvisMobile?.isPhone()) {
        globalThis.JarvisMobile.openVoice();
        return;
      }
      const open = document
        .querySelector(".work-grid")
        .classList.toggle("brain-chat-open");
      conversation.textContent = open
        ? "Hide conversation"
        : "Show conversation";
      conversation.setAttribute("aria-expanded", String(open));
    });
    conversation.setAttribute("aria-expanded", "false");
    tabs.append(conversation);
    tabs.append(button("All knowledge", () => notes()));
    box.append(tabs, content);
    await map();
    return box;
  }
  function dateKey(date) {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/Denver",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  }
  function addDay(key, n) {
    const d = new Date(key + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  async function calendar() {
    const box = el("section");
    box.append(
      intro(
        "Make room for what matters.",
        "Your HP calendar, in Denver time. Review a meeting before making any change.",
      ),
    );
    let mode = "week",
      anchor = dateKey(new Date()),
      epoch = 0;
    const toolbar = el("div", undefined, "workspace-toolbar"),
      label = el("h2"),
      content = el("div"),
      coverage = el("p", undefined, "calendar-coverage");
    async function draw() {
      const request = ++epoch;
      const date = new Date(anchor + "T12:00:00Z");
      let start, days;
      if (mode === "month") {
        const first = anchor.slice(0, 7) + "-01";
        start = addDay(first, -new Date(first + "T12:00:00Z").getUTCDay());
        days = 42;
      } else {
        start = addDay(anchor, -((date.getUTCDay() + 6) % 7));
        days = 7;
      }
      label.textContent =
        mode === "month"
          ? date.toLocaleDateString("en-US", {
              month: "long",
              year: "numeric",
              timeZone: "UTC",
            })
          : "Week of " +
            new Date(start + "T12:00:00Z").toLocaleDateString("en-US", {
              month: "long",
              day: "numeric",
              timeZone: "UTC",
            });
      content.replaceChildren(el("p", "Loading calendar…"));
      const data = await api(`/calendar/range?start=${start}&days=${days}`);
      if (request !== epoch) return;
      coverage.textContent = `Saved Outlook coverage is partial. Last sync: ${time(data.last_sync)}. Empty days may not have been synced.${data.truncated ? " Some events are outside this bounded view." : ""}`;
      const grid = el("div", undefined, "calendar-grid " + mode);
      grid.setAttribute(
        "aria-label",
        mode === "month" ? "Month calendar" : "Week calendar",
      );
      for (let i = 0; i < days; i++) {
        const key = addDay(start, i),
          cell = el(
            "section",
            undefined,
            "calendar-day" +
              (key === dateKey(new Date()) ? " calendar-today" : ""),
          );
        cell.append(
          el(
            "h3",
            new Date(key + "T12:00:00Z").toLocaleDateString("en-US", {
              weekday: "short",
              month: "short",
              day: "numeric",
              timeZone: "UTC",
            }),
          ),
        );
        const events = data.events.filter(
          (e) =>
            dateKey(new Date(e.occurred_at)) <= key &&
            dateKey(new Date(new Date(e.ends_at).getTime() - 1)) >= key,
        );
        for (const e of mode === "month" ? events.slice(0, 3) : events) {
          const hour = e.metadata.isAllDay
            ? "All day"
            : new Date(e.occurred_at).toLocaleTimeString("en-US", {
                timeZone: "America/Denver",
                hour: "numeric",
                minute: "2-digit",
              });
          const event = button(
            `${hour} · ${e.title}`,
            () => {
              showDetail(
                e.title,
                `${time(e.occurred_at)} to ${time(e.ends_at)}\n${e.metadata.isCancelled ? "Cancelled\n" : ""}${e.body || ""}`,
              );
              if (!e.metadata.isCancelled)
                document.querySelector("#detail-body").append(
                  button("Prepare me", async (event) => {
                    const trigger = event.currentTarget;
                    trigger.disabled = true;
                    trigger.textContent = "Preparing briefing…";
                    try {
                      const prepared = await api("/meetings/prepare", {
                        id: e.id,
                      });
                      message(
                        "assistant",
                        prepared.reply,
                        prepared.sources || [],
                      );
                      document.querySelector("#detail").close();
                      toast("Meeting briefing is ready in your conversation.");
                    } finally {
                      trigger.disabled = false;
                      trigger.textContent = "Prepare me";
                    }
                  }),
                );
            },
            "calendar-event" + (e.metadata.isCancelled ? " cancelled" : ""),
          );
          event.title = `${hour} · ${e.title}`;
          cell.append(event);
        }
        if (mode === "month" && events.length > 3)
          cell.append(
            button(
              `+${events.length - 3} more`,
              () => {
                anchor = key;
                mode = "week";
                draw();
              },
              "calendar-more",
            ),
          );
        if (!events.length) cell.append(el("small", "No saved events"));
        grid.append(cell);
      }
      content.replaceChildren(grid);
    }
    const move = (direction) => {
      if (mode === "month") {
        const date = new Date(anchor.slice(0, 7) + "-01T12:00:00Z");
        date.setUTCMonth(date.getUTCMonth() + direction);
        anchor = date.toISOString().slice(0, 10);
      } else anchor = addDay(anchor, direction * 7);
      draw();
    };
    toolbar.append(
      button("Previous", () => move(-1)),
      button("Today", () => {
        anchor = dateKey(new Date());
        draw();
      }),
      button("Next", () => move(1)),
      button("Week", () => {
        mode = "week";
        draw();
      }),
      button("Month", () => {
        mode = "month";
        draw();
      }),
      button("Plan meeting", () => meeting()),
    );
    box.append(toolbar, label, coverage, content);
    await draw();
    return box;
  }
  async function meeting(proposal = null) {
    const dialog = document.querySelector("#detail"),
      body = document.querySelector("#detail-body");
    dialog.classList.remove("graph-dialog");
    document.querySelector("#detail-title").textContent = "Plan a meeting";
    const form = el("form", undefined, "brain-editor"),
      fields = {};
    for (const [key, name, type] of [
      ["subject", "Meeting title", "text"],
      ["start", "Starts (your device timezone)", "datetime-local"],
      ["end", "Ends (your device timezone)", "datetime-local"],
      ["attendees", "Attendee email addresses, separated by commas", "text"],
      ["body", "Agenda", "textarea"],
    ]) {
      const label = el("label", name),
        input = el(type === "textarea" ? "textarea" : "input");
      if (type !== "textarea") input.type = type;
      input.required = ["subject", "start", "end"].includes(key);
      input.setAttribute("aria-label", name);
      label.append(input);
      form.append(label);
      fields[key] = input;
    }
    if (proposal) {
      const p = proposal.payload;
      fields.subject.value = p.subject;
      fields.body.value = p.body?.content || "";
      fields.attendees.value = (p.attendees || [])
        .map((a) => a.emailAddress.address)
        .join(", ");
      for (const key of ["start", "end"]) {
        const d = new Date(p[key].dateTime);
        fields[key].value = new Date(
          d.getTime() - d.getTimezoneOffset() * 60000,
        )
          .toISOString()
          .slice(0, 16);
      }
    }
    const save = el("button", "Prepare for review", "primary"),
      feedback = el("p");
    form.append(
      el(
        "p",
        "This creates a proposal in JARVIS. Nothing is added to Outlook and no invitations are sent until the exact action is approved.",
      ),
      save,
      feedback,
    );
    form.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        await api("/calendar/proposals", {
          replaces: proposal?.id,
          subject: fields.subject.value,
          start: new Date(fields.start.value).toISOString(),
          end: new Date(fields.end.value).toISOString(),
          attendees: fields.attendees.value
            .split(",")
            .map((x) => x.trim())
            .filter(Boolean),
          body: fields.body.value,
        });
        dialog.close();
        navigate("Approvals");
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    };
    body.replaceChildren(form);
    dialog.showModal();
  }
  function reviewable(action) {
    const p = action.payload;
    if (action.kind === "calendar.create")
      return !!(
        p &&
        typeof p.subject === "string" &&
        p.subject.trim() &&
        Number.isFinite(Date.parse(p.start?.dateTime)) &&
        Number.isFinite(Date.parse(p.end?.dateTime)) &&
        Array.isArray(p.attendees) &&
        p.attendees.every((a) => typeof a.emailAddress?.address === "string")
      );
    if (["email.send", "email.save_draft"].includes(action.kind))
      return !!(
        p?.message &&
        typeof p.message.subject === "string" &&
        typeof p.message.body?.content === "string" &&
        Array.isArray(p.message.toRecipients) &&
        p.message.toRecipients.length &&
        p.message.toRecipients.every(
          (a) => typeof a.emailAddress?.address === "string",
        )
      );
    return true;
  }
  async function email(proposal) {
    const dialog = document.querySelector("#detail"),
      body = document.querySelector("#detail-body");
    dialog.classList.remove("graph-dialog");
    document.querySelector("#detail-title").textContent = "Edit email proposal";
    const form = el("form", undefined, "brain-editor"),
      fields = {};
    const m = proposal.payload.message;
    for (const [key, title, value, limit] of [
      ["subject", "Email subject", m.subject, 500],
      [
        "recipients",
        "Recipient email addresses",
        m.toRecipients.map((r) => r.emailAddress.address).join(", "),
        3000,
      ],
      ["body", "Email content", m.body.content, 20000],
    ]) {
      const label = el("label", title),
        input = el(key === "body" ? "textarea" : "input");
      input.required = true;
      input.maxLength = limit;
      input.value = value;
      input.setAttribute("aria-label", title);
      label.append(input);
      form.append(label);
      fields[key] = input;
    }
    const save = el("button", "Replace proposal for review", "primary"),
      status = el("p");
    status.setAttribute("role", "status");
    form.append(
      el(
        "p",
        "Editing cancels the old approval and creates a new proposal. Nothing is sent or saved to Outlook.",
      ),
      save,
      status,
    );
    form.onsubmit = async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        await api(`/approvals/${proposal.id}/edit-email`, {
          subject: fields.subject.value,
          body: fields.body.value,
          recipients: fields.recipients.value
            .split(",")
            .map((x) => x.trim())
            .filter(Boolean),
          digest: proposal.digest,
        });
        dialog.close();
        navigate("Approvals");
      } catch (error) {
        status.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    };
    body.replaceChildren(form);
    dialog.showModal();
  }
  return { brain, calendar, meeting, email, dateKey, addDay, reviewable };
})();
