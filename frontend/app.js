"use strict";
const $ = (s) => document.querySelector(s);
const phoneLayout = matchMedia(
  "(max-width: 760px), (hover: none) and (pointer: coarse) and (max-width: 1024px)",
);
const views = [
  ["Today", "◷"],
  ["Inbox Intelligence", "↗", "Inbox"],
  ["Calendar", "▦"],
  ["Second Brain", "◈"],
  ["Approvals", "◇", "Review"],
];
const secondaryViews = [
  ["Account Setup", "◎"],
  ["Tasks", "✓"],
  ["People", "◎"],
  ["Projects", "▧"],
  ["Writing", "✎"],
  ["Documents", "▤"],
  ["Memory", "◈"],
  ["JARVIS Activity", "≋"],
  ["System Health", "⌁"],
  ["Settings", "⚙"],
];
let current =
    location.hash === "#setup"
      ? "Account Setup"
      : phoneLayout.matches
        ? "Voice"
        : "Today",
  responseEvidenceEnabled = false,
  health = {},
  generation = 0;
const orbStatus = {
  enabled: false,
  online: false,
  ready: false,
  urgent: false,
  busy: false,
  activity: "",
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  setTimeout(() => ($("#toast").hidden = true), 5000);
}
async function api(path, body) {
  const r = await fetch("/api" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-Jarvis-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try {
    data = await r.json();
  } catch {
    data = { detail: "The service did not return a valid response." };
  }
  if (r.status === 401) {
    await stopVoice();
    $("#workspace").hidden = true;
    $("#login").hidden = false;
  }
  if (!r.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : "The request could not be completed.",
    );
  return data;
}
function time(value) {
  return value
    ? new Date(value).toLocaleString("en-US", {
        timeZone: "America/Denver",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Not set";
}
function empty(title, body) {
  const n = el("div", undefined, "empty");
  n.append(el("strong", title), el("span", body));
  return n;
}
function heading(title, sub) {
  const n = el("div", undefined, "section-heading");
  n.append(el("h2", title));
  if (sub) n.append(el("small", sub));
  return n;
}
function intro(title, description) {
  const box = document.createDocumentFragment();
  box.append(
    el("div", "DEMO’S WORKSPACE", "eyebrow"),
    el("h1", title),
    el("p", description, "intro"),
  );
  return box;
}
function showDetail(title, body) {
  $("#detail").classList.remove("graph-dialog");
  $("#detail-title").textContent = title;
  $("#detail-body").textContent = body;
  $("#detail").showModal();
}
function reviewCommitment(record) {
  $("#detail-title").textContent = "Review commitment";
  const body = $("#detail-body");
  body.replaceChildren(
    el(
      "p",
      "Check the promise, owner and deadline against the source. This updates JARVIS's work tracker; it does not change Outlook or contact anyone.",
    ),
  );
  const form = el("form", undefined, "commitment-form");
  const field = (label, tag, value) => {
    const wrapper = el("label", label);
    const input = el(tag);
    input.value = value || "";
    wrapper.append(input);
    form.append(wrapper);
    return input;
  };
  const promise = field("Commitment", "textarea", record.text);
  promise.required = true;
  promise.maxLength = 3000;
  const owner = field("Who owes this?", "input", record.owner);
  owner.required = true;
  owner.maxLength = 300;
  const beneficiary = field("Who is waiting?", "input", record.beneficiary);
  beneficiary.maxLength = 300;
  const date = field(
    "Deadline (" + Intl.DateTimeFormat().resolvedOptions().timeZone + ")",
    "input",
    "",
  );
  date.type = "datetime-local";
  if (record.due_at) {
    const value = new Date(record.due_at);
    date.value = new Date(value.getTime() - value.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  }
  const clearDate = button("No confirmed deadline", () => {
    date.value = "";
  });
  clearDate.type = "button";
  form.append(clearDate);
  const status = field("Review decision", "select", "");
  for (const [value, label] of [
    ["open", "Confirm and track"],
    ["dismissed", "Dismiss this candidate"],
    ...(record.status !== "candidate" ? [["done", "Mark completed"]] : []),
  ]) {
    const option = el("option", label);
    option.value = value;
    status.append(option);
  }
  status.value = record.status === "candidate" ? "open" : record.status;
  const evidence = el("details");
  evidence.open = true;
  evidence.append(
    el("summary", "Source evidence"),
    el("strong", record.source_title || "Original source"),
    el("small", "Source date: " + time(record.source_date)),
    el(
      "p",
      record.source_excerpt ||
        "No source excerpt is available. Confirm only what you know.",
    ),
  );
  form.append(evidence);
  const save = el("button", "Save review");
  save.type = "submit";
  const feedback = el("p");
  feedback.setAttribute("role", "status");
  form.append(save, feedback);
  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      await api(`/commitments/${record.id}/review`, {
        revision: record.revision,
        status: status.value,
        owner: owner.value.trim(),
        beneficiary: beneficiary.value.trim(),
        text: promise.value.trim(),
        due_at: date.value ? new Date(date.value).toISOString() : null,
      });
      await render();
      $("#detail").close();
      toast("Commitment review saved.");
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  };
  body.append(form);
  $("#detail").showModal();
}
function card(record) {
  const n = el("article", undefined, "record");
  const top = el("div", undefined, "record-top");
  top.append(
    el("h3", record.title || record.text || record.name || record.action),
  );
  if (record.status) top.append(el("span", record.status, "pill"));
  if (record.metadata?.review_required)
    top.append(el("span", "Needs review", "pill"));
  if (record.metadata?.isCancelled) top.append(el("span", "Cancelled", "pill"));
  if (record.metadata?.isAllDay) top.append(el("span", "All day", "pill"));
  n.append(top);
  if (record.body) n.append(el("p", record.body.slice(0, 170)));
  if (record.metadata?.triage)
    n.append(
      el(
        "p",
        record.metadata.triage.factors.join(" · ") || "No priority flags",
      ),
    );
  if (record.occurred_at) n.append(el("small", time(record.occurred_at)));
  if (record.metadata?.from?.emailAddress)
    n.append(
      el(
        "small",
        record.metadata.from.emailAddress.name ||
          record.metadata.from.emailAddress.address,
      ),
    );
  if (record.metadata?.coverage === "conversation_preview")
    n.append(el("small", "Preview only; full message not saved yet."));
  if (record.metadata?.coverage === "attachment_needs_review")
    n.append(el("small", "Text unavailable. This document needs review."));
  n.tabIndex = 0;
  const open = () =>
    showDetail(
      record.title || record.name || "Evidence",
      record.body || record.text || JSON.stringify(record, null, 2),
    );
  n.onclick = open;
  n.onkeydown = (e) => {
    if (e.key === "Enter" && e.target === n) open();
  };
  if (typeof record.id === "string" && record.id.startsWith("draft:")) {
    n.append(button("Review draft", () => reviewDraft(record)));
    if (record.metadata?.review_reason)
      n.append(el("small", record.metadata.review_reason));
    if (!record.metadata?.review_required && !record.metadata?.internal_only)
      n.append(
        button("Review send proposal", async () => {
          await api(
            "/drafts/" + encodeURIComponent(record.id) + "/propose",
            {},
          );
          navigate("Approvals");
        }),
        button("Save to Outlook draft…", async () => {
          await api(
            "/drafts/" +
              encodeURIComponent(record.id) +
              "/propose?destination=outlook_draft",
            {},
          );
          navigate("Approvals");
        }),
      );
  }
  return n;
}

function reviewDraft(record) {
  showDetail(
    "Review draft",
    "Internal draft only. Saving does not send email.",
  );
  const form = el("form");
  const subject = el("input");
  subject.value = record.title;
  subject.required = true;
  subject.maxLength = 500;
  subject.setAttribute("aria-label", "Draft subject");
  const body = el("textarea");
  body.value = record.body;
  body.required = true;
  body.maxLength = 20000;
  body.rows = 12;
  body.setAttribute("aria-label", "Draft body");
  const save = el("button", "Save draft");
  save.type = "submit";
  form.append(subject, body, save);
  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      await api(`/drafts/${encodeURIComponent(record.id)}/edit`, {
        revision: record.revision,
        title: subject.value,
        body: body.value,
      });
      await render();
      $("#detail").close();
      toast("Draft saved. Nothing was sent.");
    } catch (error) {
      toast(error.message);
    } finally {
      save.disabled = false;
    }
  };
  $("#detail-body").append(form);
}
function button(text, fn, cls) {
  const n = el("button", text, cls);
  n.onclick = async (e) => {
    e.stopPropagation();
    n.disabled = true;
    try {
      await fn(e);
    } catch (err) {
      toast(err.message);
    } finally {
      n.disabled = false;
    }
  };
  return n;
}
async function showRelationship(personId) {
  const data = await api(
    `/relationships/detail?id=${encodeURIComponent(personId)}`,
  );
  if (!data.ready) {
    showDetail(
      data.name || "Person context",
      data.message || "Context is being prepared.",
    );
    return;
  }
  $("#detail-title").textContent = data.name;
  const body = $("#detail-body");
  body.replaceChildren(
    el(
      "p",
      [data.relationship, data.organization, data.title]
        .filter(Boolean)
        .join(" · ") || "Saved HP relationship",
    ),
    el(
      "p",
      `Latest saved activity: ${time(data.last_interaction)}. ${data.stale ? "Context is awaiting refresh." : ""}`,
    ),
    el("p", data.coverage),
  );
  if (data.topics.length)
    body.append(el("p", "Topics: " + data.topics.join(", ")));
  const weighting = el("details");
  weighting.append(el("summary", "Why this person is surfaced"));
  weighting.append(el("p", data.weight.meaning));
  for (const [factor, points] of Object.entries(data.weight.factors))
    if (points)
      weighting.append(el("p", `${factor.replaceAll("_", " ")}: ${points}`));
  body.append(weighting, heading("Open commitments and review candidates"));
  if (!data.commitments.length)
    body.append(
      el(
        "p",
        "No matching saved commitments. This does not prove nothing is outstanding.",
      ),
    );
  for (const row of data.commitments.slice(0, 10)) {
    const n = card(row);
    n.append(
      el(
        "small",
        `${row.owner} → ${row.beneficiary || "Unspecified"} · ${row.status} · Due ${time(row.due_at)}`,
      ),
    );
    n.append(button("Review commitment", () => reviewCommitment(row)));
    body.append(n);
  }
  body.append(heading("Linked evidence"));
  for (const source of data.sources.slice(0, 12)) {
    const n = card(source);
    n.append(el("small", `${source.kind} · ${time(source.at)}`));
    n.append(
      button("Read source", async () => {
        const record = await api(`/record?id=${encodeURIComponent(source.id)}`);
        if (!$("#detail").open) return;
        showDetail(
          record.title,
          record.body || "Only the saved preview is available.",
        );
      }),
    );
    body.append(n);
  }
  $("#detail").showModal();
}

async function showProject(identifier) {
  const data = await api(`/projects/${encodeURIComponent(identifier)}`);
  if (!data.enabled) return toast("Project context is disabled.");
  showDetail(data.name, "");
  const body = $("#detail-body");
  body.append(
    el(
      "p",
      `${data.status} · Prepared ${time(data.built_at)}${data.stale ? " · Updating" : ""}`,
    ),
    el("p", data.status_summary),
    el("small", data.coverage),
  );
  const controls = el("div", undefined, "setup-actions");
  body.append(controls);
  if (data.status === "candidate")
    controls.append(
      button(
        "Confirm active project",
        async () => {
          await api(`/projects/${encodeURIComponent(identifier)}/review`, {
            revision: data.revision,
            status: "active",
          });
          toast("Project confirmed. No external action was taken.");
          $("#detail").close();
          render();
        },
        "primary",
      ),
    );
  controls.append(
    button("Archive project", async () => {
      await api(`/projects/${encodeURIComponent(identifier)}/review`, {
        revision: data.revision,
        status: "archived",
      });
      $("#detail").close();
      render();
    }),
  );
  body.append(heading("People"));
  for (const p of data.people)
    body.append(
      el("p", `${p.name}${p.organization ? " · " + p.organization : ""}`),
    );
  body.append(heading("Next actions"));
  for (const text of data.next_actions) body.append(el("p", text));
  body.append(heading("Questions to resolve"));
  for (const text of data.open_questions) body.append(el("p", text));
  body.append(heading("Commitments and deadlines"));
  if (!data.commitments.length)
    body.append(el("p", "No linked obligations in this saved view."));
  for (const row of data.commitments.slice(0, 8)) {
    const n = card(row);
    n.append(
      el(
        "small",
        `${row.owner} → ${row.beneficiary} · ${row.status} · ${time(row.due_at)}`,
      ),
    );
    body.append(n);
  }
  body.append(heading("Risks to check"));
  if (!data.risks.length)
    body.append(
      el(
        "p",
        "No overdue confirmed obligations found here. Partial coverage cannot establish that the project is risk free.",
      ),
    );
  for (const r of data.risks.slice(0, 5))
    body.append(el("p", `${r.text} · ${r.reason}`));
  body.append(heading("Recent evidence"));
  if (data.decisions.length) {
    body.append(heading("Extracted decisions to verify"));
    for (const d of data.decisions) body.append(el("p", d.text));
  }
  for (const s of data.sources) {
    const n = card(s);
    n.append(el("small", `${s.kind} · ${time(s.at)}`));
    n.append(
      button("Read source", async () => {
        const record = await api(`/record?id=${encodeURIComponent(s.id)}`);
        if ($("#detail").open)
          showDetail(
            record.title,
            record.body || "Only a saved preview is available.",
          );
      }),
    );
    body.append(n);
  }
}

function renderCommitmentRadar(initial) {
  const wrapper = el("section", undefined, "commitment-radar");
  wrapper.append(
    heading(
      "Commitment radar",
      "Confirmed obligations stay separate from extracted candidates.",
    ),
  );
  const tabs = el("div", undefined, "command-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Commitment views");
  const list = el("div");
  list.setAttribute("role", "tabpanel");
  const more = button("Show more commitments", () =>
    draw(current, visible + 8),
  );
  let current = initial,
    visible = 8,
    request = 0;
  function draw(data, limit = 8) {
    current = data;
    visible = limit;
    list.replaceChildren(heading(data.labels[data.view]));
    list.append(el("small", data.coverage));
    if (data.truncated)
      list.append(
        el(
          "p",
          "This is a bounded view of the first 200 matches. Use the full commitment list below for older items.",
        ),
      );
    if (!data.rows.length)
      list.append(
        empty(
          "No matching commitments",
          "Candidates and incomplete sources may still need review.",
        ),
      );
    for (const row of data.rows.slice(0, limit)) {
      const n = card(row);
      n.append(
        el(
          "small",
          `${row.owner} → ${row.beneficiary || "Unspecified"} · ${row.ownership} · Due ${time(row.due_at)}`,
        ),
      );
      if (row.risk_reason) n.append(el("p", row.risk_reason));
      n.append(
        button(
          row.status === "candidate" ? "Review candidate" : "Update commitment",
          () => reviewCommitment(row),
        ),
      );
      list.append(n);
    }
    more.hidden = data.rows.length <= limit;
    for (const tab of tabs.children)
      tab.setAttribute("aria-selected", String(tab.dataset.view === data.view));
  }
  for (const [value, label] of Object.entries(initial.labels)) {
    const tab = button(label, async () => {
      const ownRequest = ++request;
      const data = await api(`/commitment-radar?view=${value}`);
      if (ownRequest === request) draw(data);
    });
    tab.dataset.view = value;
    tab.setAttribute("role", "tab");
    tabs.append(tab);
  }
  draw(initial);
  wrapper.append(tabs, list, more);
  return wrapper;
}

function renderPriorityBrief(data) {
  const root = el("section", undefined, "priority-brief");
  root.setAttribute("aria-label", "Top five priorities");
  const brief = data.priorities || { items: [], ready: false };
  const top = el("div", undefined, "priority-heading");
  top.append(
    el("span", "01 / YOUR NEXT MOVES", "hud-kicker"),
    el("span", "6H CYCLE", "cycle-badge"),
  );
  root.append(top, el("h2", "Five things worth your time."));
  const status = el(
    "p",
    brief.ready
      ? `Updated ${time(brief.built_at)} · Next ${time(brief.next_refresh)}`
      : "Preparing your priorities from saved Outlook context…",
    "priority-status",
  );
  if (brief.stale && brief.ready) status.textContent += " · Refresh pending";
  root.append(status);
  if (data.outlook_error || data.outlook_stale || !data.outlook_enabled)
    root.append(
      el(
        "p",
        "Outlook needs attention. This brief uses previously saved records.",
        "priority-warning",
      ),
    );
  const list = el("ol", undefined, "priority-list");
  for (const [i, row] of (brief.items || []).entries()) {
    const item = el("li", undefined, "priority-item");
    item.dataset.priorityId = row.id;
    const number = el(
      "span",
      String(i + 1).padStart(2, "0"),
      "priority-number",
    );
    const content = el("div", undefined, "priority-content");
    content.append(
      el(
        "span",
        row.action + (row.confidence === "candidate" ? " · Suggested" : ""),
        "priority-action",
      ),
      el("h3", row.display_title || row.title),
      el(
        "p",
        row.reason
          .replace("Review candidate: ", "")
          .replaceAll("Demo's priority topic: ", "")
          .replace("Sender is a confirmed VIP", "Priority contact")
          .replace(
            "Relevant to HP AI, higher education or research",
            "HP AI / higher education / research",
          )
          .replace(". This is not a confirmed obligation.", ""),
      ),
    );
    const foot = el("div", undefined, "priority-foot");
    foot.append(
      el(
        "small",
        row.kind === "email"
          ? row.person?.name
            ? "From " + row.person.name
            : "Outlook email"
          : row.kind === "event"
            ? time(row.due_at)
            : "Confirmed commitment",
      ),
    );
    foot.append(
      button(
        "View source",
        async () => {
          if (row.conflict_sources) {
            showDetail(
              row.display_title,
              row.conflict_sources
                .map((s) => `${time(s.due_at)} · ${s.title}`)
                .join("\n\n"),
            );
            $("#detail-body").append(
              button("Open calendar", () => {
                $("#detail").close();
                navigate("Calendar");
              }),
            );
            return;
          }
          const source = await api(
            "/record?id=" + encodeURIComponent(row.source_id),
          );
          showDetail(
            source.title,
            source.body || "Only a saved preview is available.",
          );
        },
        "source-link",
      ),
    );
    content.append(foot);
    item.append(number, content);
    list.append(item);
  }
  root.append(list);
  if (!brief.items?.length)
    root.append(
      el(
        "p",
        brief.ready
          ? "No current priorities identified in the saved context. Your inbox and calendar remain available."
          : "JARVIS is preparing this in the background. You can keep working.",
        "priority-empty",
      ),
    );
  if (brief.removed)
    root.append(
      el(
        "small",
        "Changed or resolved items were removed. Replacements arrive with the next refresh.",
      ),
    );
  root.append(
    el(
      "p",
      "Based on saved Outlook coverage. Suggested actions are not confirmed obligations.",
      "priority-provenance",
    ),
  );
  return root;
}

function renderCommandCenter(data) {
  const fragment = el("section", undefined, "command-center");
  fragment.id = "command-center";
  const mast = el("div", undefined, "command-mast");
  mast.append(
    el("span", "J / EXECUTIVE SYSTEM", "hud-kicker"),
    el("h1", "HP command center."),
    el("p", "Less noise. Clear priorities. Room to think.", "intro"),
  );
  fragment.append(mast);
  if (!data.ready) {
    fragment.append(empty("Preparing your work summary", data.message));
    return fragment;
  }
  const briefSlot = el("div");
  briefSlot.append(renderPriorityBrief(data));
  fragment.append(briefSlot);
  const shortcuts = el("div", undefined, "command-shortcuts");
  for (const [label, count, view] of [
    ["Inbox", data.counts.waiting_on_me, "Inbox Intelligence"],
    ["Prepared", data.counts.prepared, "Writing"],
    [
      "Decisions",
      data.categories.today.filter((r) => r.kind === "approval").length,
      "Approvals",
    ],
  ]) {
    const shortcut = button("", () => navigate(view));
    shortcut.append(el("strong", String(count)), el("span", label));
    shortcuts.append(shortcut);
  }
  fragment.append(shortcuts);
  const next = el("section", undefined, "next-up");
  next.append(
    el("span", "02 / ON THE HORIZON", "hud-kicker"),
    heading("Coming next", "Outlook calendar"),
  );
  const meetings = [...data.categories.today, ...data.categories.upcoming]
    .filter((r) => r.kind === "event" && new Date(r.due_at) >= new Date())
    .sort((a, b) => new Date(a.due_at) - new Date(b.due_at));
  for (const meeting of meetings.slice(0, 2)) {
    const row = button("", () => navigate("Calendar"), "next-meeting");
    row.append(
      el("small", time(meeting.due_at)),
      el("span", meeting.title),
      el("span", "↗"),
    );
    next.append(row);
  }
  if (!meetings.length)
    next.append(
      el("p", "No upcoming meetings in the saved window.", "priority-status"),
    );
  next.append(
    button("Open calendar", () => navigate("Calendar"), "source-link"),
  );
  fragment.append(next);
  const moreWork = el("details", undefined, "command-details");
  moreWork.append(el("summary", "Explore all work"));
  let alive = true,
    pending = false,
    fingerprint = JSON.stringify(data.priorities);
  async function updateBrief() {
    if (!alive || pending || document.hidden || $("#workspace").hidden) return;
    pending = true;
    try {
      const fresh = await api("/command-center");
      if (!alive || !fresh.ready) return;
      const nextFingerprint = JSON.stringify([
        fresh.priorities,
        fresh.outlook_stale,
        fresh.outlook_error,
        fresh.outlook_enabled,
      ]);
      data = fresh;
      if (nextFingerprint !== fingerprint) {
        briefSlot.replaceChildren(renderPriorityBrief(fresh));
        fingerprint = nextFingerprint;
      }
    } catch {
      if (alive)
        briefSlot.querySelector(".priority-status").textContent =
          "Connection interrupted. Showing the saved brief.";
    } finally {
      pending = false;
    }
  }
  const timer = setInterval(updateBrief, 60000);
  document.addEventListener("visibilitychange", updateBrief);
  fragment.jarvisDispose = () => {
    alive = false;
    clearInterval(timer);
    document.removeEventListener("visibilitychange", updateBrief);
  };
  const tabs = el("div", undefined, "command-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Work categories");
  const panel = el("section", undefined, "command-panel");
  panel.id = "command-work-panel";
  panel.setAttribute("role", "tabpanel");
  const draw = (key, limit = 5) => {
    panel.replaceChildren(heading(data.labels[key]));
    for (const tab of tabs.children)
      tab.setAttribute("aria-selected", String(tab.dataset.category === key));
    const rows = data.categories[key];
    if (!rows.length)
      panel.append(
        empty(
          "Nothing identified in saved work",
          "Partial mailbox coverage means this is not proof that nothing is outstanding.",
        ),
      );
    rows.slice(0, limit).forEach((row) => {
      const item = el("article", undefined, "record");
      item.append(
        el("h3", row.title),
        el(
          "span",
          row.confidence === "candidate" ? "Review candidate" : "Confirmed",
          "pill",
        ),
        el("p", row.reason),
      );
      if (row.owner) item.append(el("small", "Owner: " + row.owner));
      if (row.beneficiary)
        item.append(el("small", "Waiting: " + row.beneficiary));
      if (row.due_at) item.append(el("small", time(row.due_at)));
      if (row.kind === "approval")
        item.append(button("Review decision", () => navigate("Approvals")));
      else
        item.append(
          button("View evidence", async () => {
            const source = await api(
              "/record?id=" + encodeURIComponent(row.source_id),
            );
            showDetail(
              source.title,
              source.body || "Only a saved preview is available.",
            );
          }),
        );
      if (row.kind === "commitment")
        item.append(button("Review commitment", () => navigate("Tasks")));
      if (row.kind === "draft")
        item.append(
          button("Open draft", async () => {
            const source = await api(
              "/record?id=" + encodeURIComponent(row.id),
            );
            showDetail(source.title, source.body);
          }),
        );
      panel.append(item);
    });
    if (rows.length > limit)
      panel.append(button("Show more work", () => draw(key, limit + 5)));
  };
  (data.category_order || Object.keys(data.labels)).forEach((key) => {
    const label = data.labels[key];
    const tab = button(`${label} (${data.counts[key]})`, () => draw(key));
    tab.dataset.category = key;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    tabs.append(tab);
  });
  draw("today");
  moreWork.append(tabs, panel);
  fragment.append(moreWork);
  if (data.candidate_commitments)
    moreWork.append(
      button("Review unconfirmed commitment candidates", () =>
        navigate("Tasks"),
      ),
    );
  return fragment;
}

async function showMemoryConnections(root = "actor:demo", limit = 45) {
  const data = await api(
    `/memory/graph?depth=2&limit=${limit}&root=` + encodeURIComponent(root),
  );
  if (!data.enabled) {
    toast("Memory connections are awaiting validation.");
    return;
  }
  if (data.visualization_enabled) {
    showMemoryMap(data);
    return;
  }
  const panel = el("div");
  const selected = data.nodes.find((n) => n.id === data.root);
  panel.append(
    el("p", data.coverage),
    el(
      "small",
      "Connections describe saved evidence, not confirmed business outcomes.",
    ),
  );
  if (root !== "actor:demo")
    panel.append(button("Back to Demo", () => showMemoryConnections()));
  for (const node of data.nodes.filter((n) => n.id !== data.root)) {
    const item = el("article", undefined, "record");
    const links = data.edges.filter(
      (e) =>
        (e.source === root && e.target === node.id) ||
        (e.target === root && e.source === node.id),
    );
    item.append(el("h3", node.name), el("span", node.status, "pill"));
    for (const link of links)
      item.append(
        el(
          "small",
          `${link.relationship.replaceAll("_", " ")} · ${link.status}`,
        ),
      );
    if (node.summary) item.append(el("p", node.summary));
    if (node.expandable !== false)
      item.append(
        button("Explore connections", () => showMemoryConnections(node.id)),
      );
    panel.append(item);
  }
  if (data.nodes.length === 1)
    panel.append(
      el("p", "No linked evidence is available in this neighborhood."),
    );
  if (data.truncated)
    panel.append(
      el(
        "small",
        "Showing a bounded selection. Explore a specific connection for more.",
      ),
    );
  showDetail("Memory connections · " + selected.name, "");
  $("#detail-body").replaceChildren(panel);
}

function showMemoryMap(data) {
  const selected = data.nodes.find((n) => n.id === data.root);
  showDetail(
    data.evidence_only
      ? "Evidence for this answer"
      : "Memory connections · " + selected.name,
    "",
  );
  $("#detail").classList.add("graph-dialog");
  const panel = $("#detail-body"),
    controls = el("div", undefined, "graph-controls"),
    split = el("div", undefined, "graph-split"),
    inspector = el("article", undefined, "graph-inspector record");
  const explanation = el("details");
  explanation.append(
    el("summary", "About this memory map"),
    el("p", data.meaning),
    el("p", data.coverage),
  );
  panel.append(
    el(
      "small",
      `${data.evidence_only ? "Answer evidence" : "Saved work connections"} · Snapshot: ${time(data.as_of)}. Candidates need review.`,
    ),
    explanation,
  );
  const search = el("input");
  search.type = "search";
  search.maxLength = 500;
  search.placeholder = "Find a person, project or saved memory";
  search.setAttribute("aria-label", "Search memory graph");
  const results = el("div", undefined, "graph-search-results");
  const form = el("form", undefined, "graph-search");
  const searchButton = button("Search", async () => {
    if (!search.value.trim()) return;
    results.textContent = "Searching saved names and memories…";
    const found = await api(
      "/memory/graph/search?q=" + encodeURIComponent(search.value.trim()),
    );
    results.replaceChildren();
    if (!found.nodes.length)
      results.append(el("p", "No current saved matches. Coverage is partial."));
    found.nodes.forEach((n) =>
      results.append(
        button(`${n.type}: ${n.name}`, () =>
          showMemoryConnections(n.id, data.limit),
        ),
      ),
    );
  });
  searchButton.type = "button";
  form.onsubmit = (e) => {
    e.preventDefault();
    searchButton.click();
  };
  form.append(search, searchButton);
  const types = new Set(data.nodes.map(JarvisGraph.category));
  const recent = el("select");
  recent.setAttribute("aria-label", "Memory activity range");
  for (const [value, text] of [
    ["all", "All saved activity"],
    ["recent", "Dated activity in last 30 days"],
  ]) {
    const option = el("option", text);
    option.value = value;
    recent.append(option);
  }
  const capacity = el("select");
  capacity.setAttribute("aria-label", "Visible node limit");
  [30, 45, 75].forEach((value) => {
    const option = el("option", `${value} nodes maximum`);
    option.value = value;
    capacity.append(option);
  });
  capacity.value = String(data.limit);
  capacity.onchange = () =>
    showMemoryConnections(data.root, Number(capacity.value)).catch((e) =>
      toast(e.message),
    );
  const count = el("small");
  count.id = "graph-count";
  const list = el("details");
  list.append(el("summary", "Browse nodes as a list"));
  const listBody = el("div", undefined, "graph-node-list");
  list.append(listBody);
  function inspect(n, view) {
    inspector.replaceChildren(
      el("h3", n.name),
      el("span", `${n.type} · ${n.status}`, "pill"),
    );
    if (n.summary) inspector.append(el("p", n.summary));
    const m = n.metadata || {};
    if (m.selected_for_response)
      inspector.append(
        el(
          "small",
          `Source [${m.response_source_number}] selected for this response. Saved excerpt, not a fresh reread.`,
        ),
      );
    for (const [key, label] of [
      ["owner", "Owner"],
      ["beneficiary", "Owed to"],
      ["due_at", "Due"],
      ["coverage", "Source coverage"],
    ])
      if (m[key])
        inspector.append(
          el("small", `${label}: ${key === "due_at" ? time(m[key]) : m[key]}`),
        );
    if (n.confidence != null)
      inspector.append(
        el("small", `Recorded confidence: ${Math.round(n.confidence * 100)}%`),
      );
    if (n.activity_at)
      inspector.append(el("small", `Saved activity: ${time(n.activity_at)}`));
    if (n.expandable !== false && n.id !== data.root)
      inspector.append(
        button("Explore connections", () =>
          showMemoryConnections(n.id, data.limit),
        ),
      );
    if (n.id.startsWith("person:"))
      inspector.append(
        button("Open person profile", () => showRelationship(n.id.slice(7))),
      );
    if (n.id.startsWith("project:"))
      inspector.append(
        button("Open project", () => showProject(n.id.slice(8))),
      );
    const links = view.edges.filter(
      (e) => e.source === n.id || e.target === n.id,
    );
    inspector.append(el("h4", `${links.length} visible relationships`));
    const sources = new Set();
    for (const edge of links) {
      const from = view.nodes.find((node) => node.id === edge.source);
      const to = view.nodes.find((node) => node.id === edge.target);
      inspector.append(
        el(
          "p",
          `${from?.name || "Saved node"} → ${edge.relationship.replaceAll("_", " ")} → ${to?.name || "Saved node"}\n${edge.status}`,
        ),
      );
      if (
        edge.provenance &&
        !edge.provenance.startsWith("person:") &&
        edge.provenance !== "demo:direct"
      )
        sources.add(edge.provenance);
    }
    if (
      n.id.startsWith("record:") ||
      n.id.startsWith("memory:") ||
      n.type === "decision"
    ) {
      if (m.source_id && m.source_id !== "demo:direct")
        sources.add(m.source_id);
    }
    for (const id of [...sources].slice(0, 5))
      inspector.append(
        button("Read supporting source", async () => {
          const record = await api("/record?id=" + encodeURIComponent(id));
          showDetail(record.title, record.body);
          $("#detail-body").append(
            button("Back to memory map", () =>
              data.evidence_only
                ? showResponseEvidence(data.message_id)
                : showMemoryConnections(data.root, data.limit),
            ),
          );
        }),
      );
  }
  const map = JarvisGraph.create(data, inspect);
  function redraw() {
    const filtered = JarvisGraph.filtered(
      data,
      types,
      recent.value === "recent",
    );
    map.draw(filtered);
    count.textContent = `${filtered.nodes.length} nodes · ${filtered.edges.length} relationships${data.truncated ? " · Bounded selection" : ""}`;
    listBody.replaceChildren();
    filtered.nodes.forEach((n) =>
      listBody.append(
        button(`${n.type}: ${n.name}`, () => inspect(n, filtered)),
      ),
    );
    inspect(
      filtered.nodes.find((n) => n.id === data.root),
      filtered,
    );
  }
  const filters = el("div", undefined, "graph-filters");
  [...types].sort().forEach((type) => {
    const label = el("label"),
      check = el("input");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", `Show ${type} nodes`);
    check.onchange = () => {
      if (check.checked) types.add(type);
      else types.delete(type);
      redraw();
    };
    label.style.color = JarvisGraph.palette[type];
    label.append(check, el("span", type));
    filters.append(label);
  });
  recent.onchange = redraw;
  controls.append(
    recent,
    button("Zoom in", () => map.zoom(0.2)),
    button("Zoom out", () => map.zoom(-0.2)),
    button("Reset view", () => map.reset()),
  );
  if (!data.evidence_only) controls.append(capacity);
  if (!data.evidence_only && data.root !== "actor:demo")
    controls.append(button("Back to Demo", () => showMemoryConnections()));
  split.append(map.element, inspector);
  panel.append(
    ...(data.evidence_only ? [] : [form, results]),
    controls,
    filters,
    count,
    split,
    el(
      "small",
      "Positions group node types; distance is not a confidence score. Dashed links are candidate associations. Drag empty space to pan.",
    ),
    list,
  );
  redraw();
}

async function showResponseEvidence(messageId) {
  const data = await api(
    "/memory/evidence?message_id=" + encodeURIComponent(messageId),
  );
  if (!data.enabled) return toast("Answer evidence is awaiting validation.");
  showMemoryMap(data);
}

async function showMemoryViews() {
  const data = await api("/memory/views");
  if (!data.enabled) {
    toast("Generated notes are awaiting validation.");
    return;
  }
  showDetail("Generated memory notes", "");
  const panel = $("#detail-body");
  panel.append(
    el(
      "p",
      "Private Markdown snapshots of saved work. JARVIS checks freshness before opening a note.",
    ),
  );
  if (data.pending)
    panel.append(el("small", `${data.pending} notes await idle preparation.`));
  for (const view of data.views) {
    const row = el("article", undefined, "record");
    row.dataset.entityId = view.entity_id;
    row.append(
      el("h3", view.title.replace(/\s+/g, " ").slice(0, 120)),
      el(
        "small",
        `${view.kind} · ${time(view.updated_at)}${view.stale ? " · Refresh pending" : ""}`,
      ),
    );
    if (!view.stale)
      row.append(
        button("Open Markdown note", async () => {
          const note = await api(
            "/memory/views/read?kind=" +
              encodeURIComponent(view.kind) +
              "&id=" +
              encodeURIComponent(view.entity_id),
          );
          showDetail(note.title, note.markdown);
          $("#detail-body").append(
            button("Download Markdown", () => {
              const url = URL.createObjectURL(
                new Blob([note.markdown], {
                  type: "text/markdown;charset=utf-8",
                }),
              );
              const link = el("a");
              link.href = url;
              link.download = note.filename;
              link.click();
              setTimeout(() => URL.revokeObjectURL(url), 1000);
            }),
            button("Back to notes", showMemoryViews),
          );
        }),
      );
    panel.append(row);
  }
  if (!data.views.length)
    panel.append(el("p", "Notes will appear as idle preparation finishes."));
}

async function render() {
  globalThis.JarvisMobile?.sync(current);
  $(".work-grid").classList.toggle("command-mode", current === "Today");
  $(".work-grid").classList.toggle("setup-mode", current === "Account Setup");
  $(".work-grid").classList.toggle("brain-mode", current === "Second Brain");
  $(".work-grid").classList.remove("brain-chat-open");
  const epoch = ++generation;
  const main = $("#main");
  for (const child of main.children) child.jarvisDispose?.();
  main.replaceChildren(intro(current, "Loading your workspace…"));
  $("#section-name").textContent = current;
  document.querySelectorAll("nav button").forEach((n) => {
    n.classList.toggle("selected", n.dataset.view === current);
    n.setAttribute(
      "aria-current",
      n.dataset.view === current ? "page" : "false",
    );
  });
  try {
    const fragment = document.createDocumentFragment();
    if (current === "Voice") {
      fragment.append(intro("JARVIS", "Your voice. Your context."));
    } else if (current === "Account Setup") {
      fragment.append(await renderAccountSetup());
    } else if (current === "Second Brain") {
      fragment.append(await JarvisWorkspace.brain());
    } else if (current === "Calendar") {
      fragment.append(await JarvisWorkspace.calendar());
    } else if (current === "Today") {
      const command = await api("/command-center");
      if (command.enabled) {
        orbStatus.urgent =
          !command.stale &&
          (command.categories?.at_risk || []).some(
            (item) =>
              item.kind === "commitment" &&
              item.confidence === "confirmed" &&
              item.priority === 0,
          );
        fragment.append(renderCommandCenter(command));
      } else {
        const data = await api("/today");
        health = await api("/health");
        fragment.append(
          intro(
            "What deserves your attention.",
            "Your HP workday, with the important work brought forward.",
          ),
        );
        const hero = el("section", undefined, "hero");
        hero.append(
          el(
            "span",
            data.outlook_sync ? "WORKDAY BRIEF" : "OUTLOOK CONNECTION REQUIRED",
            "tag",
          ),
          el(
            "h2",
            data.outlook_sync
              ? `${data.emails.length} email threads to review. ${data.commitments.filter((item) => item.status === "open").length} confirmed commitments.`
              : "Your context is the missing piece.",
          ),
          el(
            "p",
            data.outlook_sync
              ? "Ask JARVIS to prioritize the work, prepare the next meeting, or review a response."
              : "JARVIS is running locally. Connect HP Outlook to build your briefing from real email and meetings.",
          ),
        );
        hero.append(
          button(
            data.outlook_sync ? "Open inbox" : "Connect Outlook",
            () =>
              data.outlook_sync
                ? navigate("Inbox Intelligence")
                : navigate("Account Setup"),
            "primary",
          ),
        );
        fragment.append(hero);
        const metrics = el("div", undefined, "metrics");
        [
          ["Email threads to review", data.emails.length],
          ["Calendar items · 48h", data.events.length],
          ["Awaiting your decision", data.approvals.length],
        ].forEach(([label, count]) => {
          const n = el("div", undefined, "metric");
          n.append(
            el(
              "strong",
              data.outlook_sync || label.includes("decision")
                ? String(count)
                : "—",
            ),
            el("span", label),
          );
          metrics.append(n);
        });
        fragment.append(
          metrics,
          heading(
            "Needs review",
            data.outlook_sync
              ? "Last synced " + time(data.outlook_sync)
              : "Not connected",
          ),
        );
        fragment.append(
          ...(data.emails.length
            ? data.emails.slice(0, 5).map(card)
            : [
                empty(
                  data.outlook_sync
                    ? "No priority emails in the current index"
                    : "Your mailbox has not been read",
                  data.outlook_sync
                    ? "Ask JARVIS to search if you are looking for a specific thread."
                    : "Important requests, deadlines and prepared responses will appear after authorized Outlook sync.",
                ),
              ]),
        );
        fragment.append(heading("Coming next"));
        fragment.append(
          ...(data.events.length
            ? data.events.slice(0, 5).map(card)
            : [
                empty(
                  "No meetings available",
                  data.outlook_sync
                    ? "Nothing in the next 48 hours in your current index."
                    : "Your calendar will appear when Outlook is connected.",
                ),
              ]),
        );
      }
      const researchDetails = el("details", undefined, "command-details");
      researchDetails.append(
        el("summary", "AI & market intelligence"),
        await JarvisResearch.panel(),
      );
      fragment.append(researchDetails);
    } else if (current === "System Health") {
      health = await api("/health");
      fragment.append(
        intro("System health.", "Live readiness of your private assistant."),
      );
      const rows = [
        ["Database", health.database ? "ready" : "unavailable"],
        ...Object.entries(health.models).map(([k, v]) => [
          k[0].toUpperCase() + k.slice(1) + " model",
          v,
        ]),
        ["Outlook", health.outlook.stale ? "disconnected or stale" : "synced"],
        [
          "Telegram",
          health.telegram.configured
            ? "configured; live test required"
            : "not connected",
        ],
        [
          "Phone",
          health.phone.configured
            ? "configured; live test required"
            : "not connected",
        ],
        ["Speech recognition", health.voice.stt],
        ["Speech generation", health.voice.tts],
        ["Demo mode", health.demo_mode ? "on" : "off"],
      ];
      for (const [name, state] of rows) {
        const n = el("div", undefined, "health-row");
        n.append(
          el("span", name),
          el(
            "span",
            state,
            "pill" + (/ready|synced|off/.test(state) ? "" : " warning"),
          ),
        );
        fragment.append(n);
      }
      const workload = await api("/system/workload");
      const details = el("details", undefined, "settings-block");
      details.append(el("summary", "Workload and resource protection"));
      details.append(
        el(
          "p",
          workload.background_pause_reason
            ? "Background work paused: " +
                workload.background_pause_reason.replaceAll("_", " ")
            : "Background work may run when you are not interacting.",
        ),
      );
      if (workload.memory_available_bytes !== null)
        details.append(
          el(
            "p",
            `${(workload.memory_available_bytes / 1024 ** 3).toFixed(1)} GiB memory available. One background model request at a time.`,
          ),
        );
      details.append(
        el(
          "p",
          workload.interactive_active
            ? "Your conversation has priority."
            : "No active conversation request.",
        ),
      );
      fragment.append(details);
    } else if (current === "Settings") {
      health = await api("/health");
      fragment.append(
        intro(
          "Connections & boundaries.",
          "Your accounts, your authorization. External actions require your decision.",
        ),
      );
      const block = el("section", undefined, "settings-block");
      block.append(
        el("h2", "Your connected accounts"),
        el(
          "p",
          "Review Outlook, Telegram and telephone connections, permissions and system readiness in one place.",
        ),
        button(
          "Open Account Setup",
          () => navigate("Account Setup"),
          "primary",
        ),
      );
      fragment.append(block);
      const documents = el("section", undefined, "settings-block");
      const fileInput = el("input");
      fileInput.type = "file";
      fileInput.accept = ".pdf,.docx,.pptx,.xlsx,.txt,.md,.vtt,.csv,.tsv";
      fileInput.setAttribute("aria-label", "Document or Copilot transcript");
      const meetingOptions = await api("/meetings/options");
      const meetingSelect = el("select");
      meetingSelect.setAttribute(
        "aria-label",
        "Link source to a saved meeting",
      );
      const unlinked = el("option", "No meeting link");
      unlinked.value = "";
      meetingSelect.append(unlinked);
      for (const m of meetingOptions.meetings) {
        const option = el("option", `${time(m.occurred_at)} · ${m.title}`);
        option.value = m.id;
        meetingSelect.append(option);
      }
      const transcript = el("input");
      transcript.type = "checkbox";
      transcript.setAttribute("aria-label", "This is a meeting transcript");
      const transcriptLabel = el("label");
      transcriptLabel.append(
        transcript,
        document.createTextNode(" This is a meeting transcript"),
      );
      documents.append(
        el("h2", "Documents & Copilot transcripts"),
        el(
          "p",
          "Upload a source for local retrieval and extraction. Initial limit: 1.5 MB. Extracted commitments remain candidates until reviewed.",
        ),
        fileInput,
        button("Import document", async () => {
          const file = fileInput.files[0];
          if (!file) throw Error("Select a document first.");
          if (file.size > 1500000)
            throw Error("The initial upload limit is 1.5 MB.");
          const form = new FormData();
          form.append("file", file);
          if (meetingSelect.value)
            form.append("meeting_id", meetingSelect.value);
          if (transcript.checked) form.append("transcript", "true");
          const response = await fetch("/api/documents", {
            method: "POST",
            headers: { "X-Jarvis-Request": "1" },
            body: form,
          });
          const result = await response.json();
          if (!response.ok)
            throw Error(result.detail || "Document import failed.");
          fileInput.value = "";
          toast("Document indexed. Structured extraction is queued.");
        }),
      );
      if (meetingOptions.enabled) {
        const submit = documents.lastElementChild;
        for (const control of [
          transcriptLabel,
          el(
            "p",
            "Optional: link a Copilot transcript or document to its saved meeting for context and source-date interpretation.",
          ),
          meetingSelect,
        ])
          documents.insertBefore(control, submit);
      }
      fragment.append(documents);
      const quiet = el("section", undefined, "settings-block");
      quiet.append(
        el("h2", "Communication"),
        el(
          "p",
          "Morning briefing: 7:00 AM, Monday through Saturday. No Sunday morning call.",
        ),
      );
      quiet.append(
        button("Text only for 8 hours", () =>
          api("/communication", {
            mode: "text_only",
            until: new Date(Date.now() + 8 * 3600000).toISOString(),
          }).then(() => toast("Text only for the next 8 hours.")),
        ),
        button("Quiet for 2 hours", () =>
          api("/communication", {
            mode: "quiet",
            until: new Date(Date.now() + 2 * 3600000).toISOString(),
          }).then(() => toast("Notifications paused for 2 hours.")),
        ),
      );
      fragment.append(quiet);
      for (const name of ["Telegram", "Phone"]) {
        const n = el("section", undefined, "settings-block");
        n.append(
          el("h2", name),
          el(
            "p",
            health[name === "Telegram" ? "telegram" : "phone"].configured
              ? "Configured. Verify the live account before relying on proactive delivery."
              : name === "Telegram"
                ? "Only your configured private Telegram identity can use JARVIS. Account connection is pending."
                : "Twilio account, dedicated number and signed HTTPS ingress are required. No calls are enabled yet.",
          ),
        );
        fragment.append(n);
      }
    } else if (current === "Approvals") {
      const data = await api("/approvals");
      const writes = await api("/outlook/write-capabilities");
      fragment.append(el("p", writes.message, "intro"));
      fragment.append(button("Prepared writing", () => navigate("Writing")));
      const preparing = await api("/mail/drafts/status");
      if (preparing.length) {
        fragment.append(
          heading(
            "Reply preparation",
            "Refresh to check readiness. Conversation has priority.",
          ),
        );
        for (const job of preparing) {
          const row = el("article", undefined, "record");
          row.append(
            el("h3", job.title),
            el(
              "p",
              job.status === "running"
                ? "Preparing your reply…"
                : job.status === "pending"
                  ? "Waiting to prepare your reply."
                  : "Preparation failed. Your source email is safe.",
            ),
          );
          if (job.status === "failed")
            row.append(
              button("Retry reply", async () => {
                await api("/mail/draft", { source_id: job.source_id });
                render();
              }),
            );
          fragment.append(row);
        }
      }
      const drafts = await api("/records/draft");
      const readyDrafts = drafts.filter(
        (d) =>
          !d.metadata?.writing_request || d.metadata.writing_state === "ready",
      );
      if (readyDrafts.length) {
        const prepared = el("details");
        prepared.open = true;
        prepared.append(el("summary", "Drafts ready for your review"));
        readyDrafts.slice(0, 12).forEach((d) => prepared.append(card(d)));
        fragment.append(prepared);
      }
      fragment.append(
        intro(
          "Your decision. Precisely scoped.",
          "Review the exact action before approving. Editing requires a fresh approval.",
        ),
      );
      if (!data.length)
        fragment.append(
          empty(
            "No decisions waiting",
            "Prepared consequential actions appear here with an expiration and a unique action ID.",
          ),
        );
      for (const a of data) {
        const n = el("article", undefined, "record");
        const reviewable = JarvisWorkspace.reviewable(a);
        n.append(
          el("h3", a.kind),
          el("span", a.status, "pill"),
          el("small", `Action ${a.id} · Expires ${time(a.expires_at)}`),
        );
        if (!reviewable)
          n.append(
            el(
              "p",
              "This older proposal is incomplete. Deny it and prepare a new proposal before taking action.",
            ),
          );
        if (a.kind === "calendar.create" && reviewable) {
          n.append(
            el("h3", a.payload.subject),
            el(
              "p",
              `${time(a.payload.start.dateTime)} to ${time(a.payload.end.dateTime)}`,
            ),
            el(
              "p",
              "Attendees: " +
                ((a.payload.attendees || [])
                  .map((x) => x.emailAddress.address)
                  .join(", ") || "Only your calendar"),
            ),
            el("p", a.payload.body?.content || ""),
          );
          if (["pending", "approved"].includes(a.status))
            n.append(button("Edit meeting", () => JarvisWorkspace.meeting(a)));
        } else if (
          ["email.send", "email.save_draft"].includes(a.kind) &&
          reviewable
        ) {
          const m = a.payload.message;
          n.append(
            el("h3", m.subject),
            el(
              "p",
              "To: " +
                m.toRecipients.map((x) => x.emailAddress.address).join(", "),
            ),
            el("p", m.body.content),
            el(
              "p",
              a.kind === "email.save_draft"
                ? "Save an unsent draft in Outlook. No delivery to recipients."
                : "Sending will deliver this exact message to the listed recipients.",
            ),
          );
          if (a.payload.packet) {
            const attachment = el("a", "Review the exact PDF attachment");
            attachment.href = `/api/content-packets/${encodeURIComponent(a.payload.packet.edition)}/download/pdf`;
            attachment.setAttribute("download", "");
            n.append(
              el(
                "p",
                `Attachment: ${a.payload.packet.filename} · ${a.payload.packet.bytes} bytes`,
              ),
              attachment,
            );
          }
          if (["pending", "approved"].includes(a.status) && !a.payload.packet)
            n.append(button("Edit email", () => JarvisWorkspace.email(a)));
        }
        const technical = el("details");
        technical.append(
          el("summary", "Exact action details"),
          el("pre", JSON.stringify(a.payload, null, 2)),
        );
        n.append(technical);
        const controls = el("div", undefined, "actions");
        if (["pending", "approved"].includes(a.status))
          for (const cmd of reviewable && a.status === "pending"
            ? ["APPROVE", "DENY"]
            : ["DENY"])
            controls.append(
              button(
                cmd === "APPROVE" ? "Approve this action" : "Deny",
                async () => {
                  await api(`/approvals/${a.id}/decision`, {
                    command: cmd,
                    digest: a.digest,
                  });
                  toast(
                    cmd === "APPROVE"
                      ? "Approved. Execution remains scoped to this action."
                      : "Action denied.",
                  );
                  render();
                },
                cmd === "APPROVE" ? "primary" : "",
              ),
            );
        if (
          a.status === "approved" &&
          reviewable &&
          (a.kind === "memory.forget" ||
            writes.executable_kinds?.includes(a.kind))
        )
          controls.append(
            button(
              "Execute approved action",
              async () => {
                await api(`/approvals/${a.id}/execute`, {});
                toast("Approved action executed.");
                render();
              },
              "primary",
            ),
          );
        n.append(controls);
        fragment.append(n);
      }
    } else if (current === "Projects") {
      const data = await api("/projects");
      fragment.append(
        intro(
          "Projects, prepared.",
          "People, sources and obligations in one saved view. Project suggestions need your confirmation.",
        ),
      );
      if (!data.enabled)
        fragment.append(
          empty(
            "Project context is disabled",
            "Saved project facts remain available in Memory.",
          ),
        );
      else {
        fragment.append(el("p", data.coverage));
        if (data.pending_sources)
          fragment.append(
            el(
              "small",
              "New evidence is being indexed in the background. Conversation has priority.",
            ),
          );
        if (!data.projects.length)
          fragment.append(
            empty(
              "Preparing project suggestions",
              "JARVIS is looking for relevant current work. Saved project facts remain in Memory.",
            ),
          );
        for (const p of data.projects) {
          const n = card(p);
          n.append(
            el(
              "small",
              `${p.status} · ${p.source_count} saved sources · ${p.people_count} people${p.stale ? " · Updating" : ""}`,
            ),
          );
          n.append(button("Open project context", () => showProject(p.id)));
          fragment.append(n);
        }
      }
    } else if (current === "Writing") {
      fragment.append(await JarvisPackets.panel());
      const data = await api("/writing");
      fragment.append(
        intro(
          "Useful work, prepared for review.",
          "Draft HP updates and briefs from saved evidence. Nothing is sent or published.",
        ),
      );
      if (!data.enabled)
        fragment.append(el("p", "Executive writing is awaiting validation."));
      else {
        const form = el("form", undefined, "record");
        const mode = el("select");
        mode.setAttribute("aria-label", "Writing mode");
        data.modes.forEach((item) => {
          const option = el("option", item.name);
          option.value = item.id;
          mode.append(option);
        });
        const description = el("small", data.modes[0].description);
        mode.onchange = () => {
          description.textContent = data.modes.find(
            (item) => item.id === mode.value,
          ).description;
        };
        const topic = el("input");
        topic.required = true;
        topic.minLength = 2;
        topic.maxLength = 300;
        topic.placeholder = "Topic, for example ZGX Nano launch readiness";
        topic.setAttribute("aria-label", "Writing topic");
        const project = el("select");
        project.setAttribute("aria-label", "Writing project");
        const any = el("option", "Search relevant saved evidence");
        any.value = "";
        project.append(any);
        const projects = await api("/projects");
        for (const item of projects.projects || []) {
          const option = el(
            "option",
            item.name + (item.status === "candidate" ? " · candidate" : ""),
          );
          option.value = item.id;
          project.append(option);
        }
        let requestId = crypto.randomUUID();
        form.oninput = () => {
          requestId = crypto.randomUUID();
        };
        const prepare = el("button", "Prepare draft");
        prepare.type = "submit";
        form.append(
          mode,
          description,
          topic,
          project,
          prepare,
          el(
            "small",
            "Preparation runs in the background and yields when you speak. Review sources, dates and ownership before using a draft.",
          ),
        );
        form.onsubmit = async (event) => {
          event.preventDefault();
          prepare.disabled = true;
          try {
            await api("/writing", {
              mode: mode.value,
              topic: topic.value,
              project_id: project.value || null,
              request_id: requestId,
            });
            requestId = crypto.randomUUID();
            await render();
            toast("Writing queued. Nothing was sent.");
          } catch (error) {
            toast(error.message);
          } finally {
            prepare.disabled = false;
          }
        };
        fragment.append(
          form,
          button("Refresh writing status", () => render()),
        );
        for (const draft of data.drafts) {
          const state = draft.metadata.writing_state;
          const record = el("article", undefined, "record");
          record.append(
            el("h3", draft.title),
            el(
              "span",
              draft.job_status === "failed"
                ? "Needs attention"
                : state === "queued"
                  ? draft.job_status === "running"
                    ? "Preparing"
                    : "Queued"
                  : state.replaceAll("_", " "),
              "pill",
            ),
          );
          if (draft.job_status === "failed")
            record.append(
              el(
                "p",
                "Preparation could not finish. Cancel this request and try again after checking System Health.",
              ),
            );
          for (const gap of draft.metadata.gaps || [])
            record.append(el("small", gap));
          if (state === "queued")
            record.append(
              button("Cancel writing request", async () => {
                await api(
                  `/writing/${encodeURIComponent(draft.id)}/cancel`,
                  {},
                );
                render();
              }),
            );
          if (state === "ready") {
            record.append(
              button("Review draft", () => reviewDraft(draft)),
              button("Read draft", () => showDetail(draft.title, draft.body)),
            );
            const evidence = el("details");
            evidence.append(el("summary", "Draft sources"));
            for (const source of draft.metadata.sources || [])
              evidence.append(
                button(`[${source.number}] ${source.title}`, async () => {
                  const current = await api(
                    "/record?id=" + encodeURIComponent(source.id),
                  );
                  showDetail(current.title, current.body);
                }),
              );
            record.append(
              evidence,
              el(
                "small",
                "Internal content only. Editing or copying does not send it. Verify current evidence before use.",
              ),
            );
          }
          fragment.append(record);
        }
      }
    } else if (current === "Memory") {
      const [data, consolidation] = await Promise.all([
        api("/memory"),
        api("/memory/consolidation"),
      ]);
      fragment.append(
        intro(
          current === "Memory"
            ? "Remembered. With evidence."
            : "Projects in context.",
          current === "Memory"
            ? "Ask “Remember this” in chat. Every memory retains its source."
            : "Project facts collected from your work and direct instructions.",
        ),
      );
      fragment.append(
        button("Explore memory connections", () => showMemoryConnections()),
        button("Generated memory notes", showMemoryViews),
      );
      if (consolidation.enabled) {
        fragment.append(
          el(
            "p",
            `${consolidation.current_summaries} source summaries prepared while idle. ${consolidation.pending} source updates await idle processing.`,
            "memory-consolidation-status",
          ),
        );
        fragment.append(
          el(
            "small",
            "Generated summaries are interpretations with source evidence. Your direct memories remain separate.",
          ),
        );
      }
      const records =
        current === "Projects"
          ? data.filter((r) => r.kind === "project")
          : data;
      if (!records.length)
        fragment.append(
          empty(
            "No memories here yet",
            "Tell JARVIS what to remember, or connect Outlook to start building context.",
          ),
        );
      for (const r of records) {
        const n = card(
          r.generated
            ? {
                ...r,
                title: r.source_title || "Prepared source summary",
                body: r.text,
                status: "Inferred summary",
              }
            : r,
        );
        n.append(
          el(
            "small",
            `Source: ${r.source_title || (r.source_id === "demo:direct" ? "You told JARVIS" : r.source_id)} · ${r.kind}${r.permanent ? " · permanent" : ""}`,
          ),
        );
        if (r.source_title)
          n.append(
            button("Read source", async () => {
              const source = await api(
                "/record?id=" + encodeURIComponent(r.source_id),
              );
              showDetail(source.title, source.body);
            }),
          );
        n.append(
          button("Forget…", async () => {
            await api(`/memory/${r.id}/forget`, {});
            toast("Review the deletion in Approvals.");
            navigate("Approvals");
          }),
        );
        fragment.append(n);
      }
    } else if (current === "People") {
      const [data, intelligence] = await Promise.all([
        api("/people"),
        api("/relationships"),
      ]);
      fragment.append(
        intro(
          "People, with context.",
          "Real relationships discovered from your authorized HP sources.",
        ),
      );
      if (intelligence.enabled) {
        fragment.append(el("p", intelligence.coverage));
        if (intelligence.pending)
          fragment.append(
            el("small", "Some profiles are being refreshed in the background."),
          );
        const profiles = el("div");
        const drawProfiles = (limit = 12) => {
          profiles.replaceChildren();
          for (const p of intelligence.profiles.slice(0, limit)) {
            const n = card(p);
            n.append(
              el("p", p.topics.join(" · ") || "Recent saved correspondence"),
            );
            n.append(
              el(
                "small",
                `Latest activity ${time(p.last_interaction)}${p.stale ? " · Awaiting refresh" : ""}`,
              ),
            );
            n.append(
              button("Relationship context", () => showRelationship(p.id)),
            );
            profiles.append(n);
          }
          if (intelligence.profiles.length > limit)
            profiles.append(
              button("Show more people", () => drawProfiles(limit + 12)),
            );
        };
        drawProfiles();
        fragment.append(profiles);
        fragment.append(
          heading(
            "Saved contacts",
            "VIP controls and other discovered identities remain available.",
          ),
        );
      }
      if (!data.length)
        fragment.append(
          empty(
            "No contacts indexed",
            "Names and relationships will come from real email and calendar records.",
          ),
        );
      const contacts = intelligence.enabled ? el("details") : fragment;
      if (intelligence.enabled)
        contacts.append(
          el("summary", "Browse saved contacts and VIP controls"),
        );
      for (const p of data) {
        const n = card(p);
        n.append(
          el("small", p.id),
          button(p.vip ? "Remove VIP" : "Make VIP", async () => {
            await api(`/people/${encodeURIComponent(p.id)}/vip`, {
              enabled: !p.vip,
            });
            render();
          }),
        );
        contacts.append(n);
      }
      if (intelligence.enabled) fragment.append(contacts);
    } else if (current === "Tasks") {
      const radar = await api("/commitment-radar");
      let data = await api("/commitments?status=active&limit=100");
      fragment.append(
        intro(
          "Commitments to keep.",
          "Source-backed commitments. Extracted candidates still need review.",
        ),
      );
      if (radar.enabled) fragment.append(renderCommitmentRadar(radar));
      if (!data.length)
        fragment.append(
          empty(
            "No commitments indexed",
            "Requests and promises will appear here with an owner, evidence and any confirmed deadline.",
          ),
        );
      const filter = el("select");
      filter.setAttribute("aria-label", "Commitment status");
      for (const [value, label] of [
        ["active", "Open and needs review"],
        ["open", "Confirmed"],
        ["candidate", "Needs review"],
        ["done", "Completed"],
        ["dismissed", "Dismissed"],
      ]) {
        const option = el("option", label);
        option.value = value;
        filter.append(option);
      }
      const list = el("div");
      const draw = () => {
        list.replaceChildren();
        data
          .filter((r) =>
            filter.value === "active"
              ? ["open", "candidate"].includes(r.status)
              : r.status === filter.value,
          )
          .forEach((r) => {
            const n = card(r);
            n.append(
              el(
                "small",
                `${r.owner} → ${r.beneficiary || "Unspecified"} · Due ${time(r.due_at)}`,
              ),
            );
            n.append(
              button(
                r.status === "candidate"
                  ? "Review candidate"
                  : "Update commitment",
                () => reviewCommitment(r),
              ),
            );
            list.append(n);
          });
      };
      let commitmentRequest = 0;
      const more = button("Load older commitments", async () => {
        const request = ++commitmentRequest;
        const next = await api(
          `/commitments?status=${filter.value}&limit=100&offset=${data.length}`,
        );
        if (request === commitmentRequest) {
          data.push(...next);
          draw();
          more.hidden = next.length < 100;
        }
      });
      more.hidden = data.length < 100;
      filter.onchange = async () => {
        const request = ++commitmentRequest;
        try {
          const next = await api(
            `/commitments?status=${filter.value}&limit=100`,
          );
          if (request === commitmentRequest) {
            data = next;
            draw();
            more.hidden = next.length < 100;
          }
        } catch (error) {
          toast(error.message);
        }
      };
      draw();
      if (radar.enabled) {
        const all = el("details");
        all.append(
          el("summary", "All commitments and candidate review"),
          filter,
          list,
          more,
        );
        fragment.append(all);
      } else fragment.append(filter, list, more);
    } else if (current === "JARVIS Activity") {
      const data = await api("/activity");
      fragment.append(
        intro(
          "An inspectable record.",
          "What JARVIS did, who authorized it, and which records were involved.",
        ),
      );
      data.forEach((r) => {
        const n = card(r);
        n.append(el("small", `${time(r.at)} · ${r.actor} · ${r.source}`));
        fragment.append(n);
      });
    } else {
      const kind =
        current === "Calendar"
          ? "event"
          : current === "Documents"
            ? "document"
            : "email";
      const data = await api(
        kind === "event"
          ? "/calendar?days=7"
          : kind === "document"
            ? "/documents"
            : "/mail",
      );
      fragment.append(
        intro(
          kind === "event"
            ? "The shape of your day."
            : kind === "document"
              ? "The evidence behind your work."
              : "Signal from your inbox.",
          kind === "event"
            ? "Upcoming meetings in Denver time. Saved Outlook coverage is partial; cancelled meetings remain labelled."
            : kind === "document"
              ? "Saved attachments and uploaded documents. Search extracted text and inspect the source email."
              : "Search saved HP mail by subject, content or sender. Results reflect partial mailbox coverage, up to 100 matches.",
        ),
      );
      const recordList = el("div");
      if (kind !== "event") {
        const form = el("form", undefined, "source-search");
        const query = el("input");
        query.type = "search";
        query.setAttribute(
          "aria-label",
          kind === "document" ? "Search saved documents" : "Search saved mail",
        );
        query.placeholder =
          kind === "document"
            ? "Find a document or topic"
            : "Find a person, project or thread";
        const filter = el("select");
        filter.setAttribute("aria-label", "Mail view");
        for (const [value, label] of [
          ["all", "All saved mail"],
          ["attention", "Priority candidates"],
          ["unread", "Unread"],
          ["sent", "Sent"],
        ]) {
          const option = el("option", label);
          option.value = value;
          filter.append(option);
        }
        const search = el("button", "Search");
        search.type = "submit";
        form.append(query);
        if (kind === "email") form.append(filter);
        form.append(search);
        let searchRequest = 0;
        const runSearch = async () => {
          const request = ++searchRequest;
          try {
            const rows = await api(
              (kind === "document" ? "/documents" : "/mail") +
                "?q=" +
                encodeURIComponent(query.value) +
                (kind === "email" ? "&view=" + filter.value : ""),
            );
            if (request === searchRequest) drawRecords(rows);
          } catch (error) {
            toast(error.message);
          }
        };
        form.onsubmit = (event) => {
          event.preventDefault();
          runSearch();
        };
        filter.onchange = runSearch;
        fragment.append(form);
        if (kind === "document")
          fragment.append(
            button("Import a document", () => navigate("Settings")),
          );
      }
      if (kind === "event") {
        const range = el("select");
        range.setAttribute("aria-label", "Calendar range");
        for (const [value, label] of [
          ["1:0", "Today"],
          ["1:1", "Tomorrow"],
          ["7:0", "Next 7 days"],
          ["14:0", "Next 14 days"],
        ]) {
          const option = el("option", label);
          option.value = value;
          range.append(option);
        }
        range.value = "7:0";
        let rangeRequest = 0;
        range.onchange = async () => {
          const request = ++rangeRequest;
          const [days, offset] = range.value.split(":");
          try {
            const rows = await api(`/calendar?days=${days}&offset=${offset}`);
            if (request === rangeRequest) drawRecords(rows);
          } catch (error) {
            toast(error.message);
          }
        };
        fragment.append(range);
      }
      const drawRecords = (rows) => {
        recordList.replaceChildren();
        if (!rows.length)
          recordList.append(
            empty(
              kind === "event"
                ? "No saved meetings in this range"
                : kind === "document"
                  ? "No saved documents match"
                  : "No saved mail matches",
              "Check Outlook connection and coverage in Account Setup before relying on an empty list.",
            ),
          );
        rows.forEach((r) => {
          const item = card(r);
          if (kind === "email")
            item.append(
              button("Draft reply", async () => {
                const result = await api("/mail/draft", { source_id: r.id });
                toast(
                  result.status === "ready"
                    ? "A draft is ready in Review. Nothing has been sent."
                    : "Preparing your reply. Refresh Review shortly; nothing will be sent.",
                );
              }),
            );
          if (kind === "document" && r.metadata?.source_id)
            item.append(
              button("Linked source", async () => {
                const source = await api(
                  "/record?id=" + encodeURIComponent(r.metadata.source_id),
                );
                showDetail(
                  source.title,
                  source.body || "Only partial source content is available.",
                );
              }),
            );
          if (kind === "document")
            item.append(
              button("Extracted notes", async () => {
                const notes = await api(
                  `/documents/insights?id=${encodeURIComponent(r.id)}`,
                );
                showDetail(r.title, notes.summary);
                const body = $("#detail-body");
                for (const decision of notes.decisions)
                  body.append(el("p", `${decision.text} · ${decision.status}`));
                if (notes.note) body.append(el("small", notes.note));
                if (notes.status === "pending")
                  body.append(
                    button("Prepare structured notes", async () => {
                      await api("/documents/notes", { id: r.id });
                      toast("Notes are queued. Conversation has priority.");
                      $("#detail").close();
                    }),
                  );
              }),
            );
          if (kind === "event" && !r.metadata?.isCancelled)
            item.append(
              button("Prepare me", async (event) => {
                const trigger = event.currentTarget;
                trigger.textContent = "Preparing briefing…";
                try {
                  const prepared = await api("/meetings/prepare", { id: r.id });
                  message("assistant", prepared.reply, prepared.sources || []);
                  toast(
                    prepared.status === "prepared"
                      ? "Meeting briefing is ready in your conversation."
                      : "Meeting sources are available; preparation needs attention.",
                  );
                } finally {
                  trigger.textContent = "Prepare me";
                }
              }),
            );
          recordList.append(item);
        });
      };
      drawRecords(data);
      fragment.append(recordList);
      if (kind === "email") {
        const drafts = await api("/records/draft");
        fragment.append(heading("Prepared responses"));
        fragment.append(
          ...(drafts.length
            ? drafts.map(card)
            : [
                empty(
                  "No drafts prepared",
                  "Important, actionable messages can become internal drafts for your review. Nothing is sent automatically.",
                ),
              ]),
        );
      }
    }
    if (epoch === generation) main.replaceChildren(fragment);
    else for (const child of fragment.children) child.jarvisDispose?.();
  } catch (error) {
    if (epoch === generation)
      main.replaceChildren(
        intro(current, "The workspace needs attention."),
        empty("Unable to load", error.message),
      );
  }
}
function navigate(name) {
  current = name;
  if (secondaryViews.some(([view]) => view === name))
    $(".workspace-more").open = true;
  history.replaceState(
    null,
    "",
    name === "Account Setup" ? "#setup" : location.pathname,
  );
  render();
}
window.addEventListener("hashchange", () => {
  if (location.hash === "#setup") {
    current = "Account Setup";
    render();
  }
});
function message(role, text, sources = [], messageId = null) {
  const article = el(
    "article",
    undefined,
    role === "user" ? "user-message" : "assistant-message",
  );
  article.append(
    el("small", role === "user" ? "DEMO" : "JARVIS"),
    el("p", text),
  );
  if (sources.length) {
    const details = el("details");
    details.append(el("summary", "Sources"));
    sources.forEach((source, index) => {
      if (source.kind === "web") {
        details.append(JarvisResearch.source(source, index + 1));
        return;
      }
      const coverage =
        {
          conversation_preview:
            "Email preview; the complete message is not included here.",
          message_body: "Excerpt from a full message saved locally.",
          calendar_summary:
            "Calendar summary; some meeting details may be missing.",
          calendar_detail: "Details read from the meeting.",
          attachment_text:
            "Text extracted from an Outlook attachment; visual details may be missing.",
          attachment_needs_review:
            "Attachment saved privately; readable text could not be extracted.",
        }[source.coverage] || "Saved work item.";
      details.append(
        button(`[${index + 1}] ${source.title}`, () =>
          showDetail(
            source.title,
            `${source.excerpt || "Source selected from saved work."}\n\nSource date: ${time(source.at)}\n${coverage}`,
          ),
        ),
      );
    });
    article.append(details);
    if (
      role === "assistant" &&
      messageId &&
      sources.some((s) => s.kind !== "web")
    ) {
      const evidenceButton = button("View answer evidence", () =>
        showResponseEvidence(messageId),
      );
      evidenceButton.classList.add("response-evidence");
      evidenceButton.hidden = !responseEvidenceEnabled;
      article.append(evidenceButton);
    }
  }
  $("#conversation").append(article);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
}
async function ask(text) {
  if ($("#send").disabled) return;
  message("user", text);
  $("#chat-input").value = "";
  $("#send").disabled = true;
  orbStatus.busy = true;
  $("#chat-status").textContent = "Checking your context…";
  try {
    const data = await api("/chat", { text });
    message("assistant", data.reply, data.sources || [], data.message_id);
    if (current === "Memory") render();
  } catch (err) {
    message("assistant", err.message);
  } finally {
    $("#send").disabled = false;
    orbStatus.busy = false;
    $("#chat-status").textContent = "One assistant. Your context.";
  }
}
async function init() {
  try {
    await api("/today");
    $("#login").hidden = true;
    $("#workspace").hidden = false;
    const history = await api("/chat");
    if (history.length) {
      $("#conversation").replaceChildren();
      history.forEach((r) =>
        message(r.role, r.content, r.metadata?.sources || [], r.id),
      );
    }
    render();
    refreshOrbHealth();
  } catch {
    $("#login").hidden = false;
    $("#workspace").hidden = true;
  }
}
[...views, ...secondaryViews].forEach(([name, icon, label]) => {
  const n = el("button");
  n.dataset.view = name;
  const glyph = el("span", icon, "nav-icon");
  glyph.setAttribute("aria-hidden", "true");
  n.append(glyph, el("span", label || name));
  n.onclick = () => navigate(name);
  $(
    views.some(([view]) => view === name)
      ? "#navigation"
      : "#secondary-navigation",
  ).append(n);
});
$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/auth/login", { token: $("#token").value });
    $("#token").value = "";
    init();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
};
$("#chat-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#chat-input").value.trim();
  if (text) ask(text);
};
$("#chat-input").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
};
document
  .querySelectorAll("[data-prompt]")
  .forEach((n) => (n.onclick = () => ask(n.dataset.prompt)));
$("#refresh").onclick = render;
$("#logout").onclick = async () => {
  await stopVoice();
  await api("/auth/logout", {});
  $("#workspace").hidden = true;
  $("#login").hidden = false;
};
$("#close-detail").onclick = () => $("#detail").close();
$("#clock").textContent = new Date().toLocaleDateString("en-US", {
  timeZone: "America/Denver",
  weekday: "short",
  month: "short",
  day: "numeric",
});
init();
const voicePanel = el("section", undefined, "voice-panel");
voicePanel.setAttribute("aria-label", "Voice conversation");
const voiceStatus = el("strong", "Voice is off");
voiceStatus.id = "voice-status";
voiceStatus.setAttribute("role", "status");
const voiceHelp = el(
  "small",
  "Speak naturally. JARVIS replies in his original British voice.",
);
const voiceActions = el("div", undefined, "voice-actions");
const voiceButton = button("Start Conversation", toggleVoice);
voiceButton.id = "voice-button";
voiceButton.setAttribute("aria-pressed", "false");
const voiceSample = button("Hear his voice", () => startVoice(false));
voiceSample.id = "voice-sample";
const voiceMute = button("Mute mic", () => {
  if (!mic) return;
  const muted = mic.getAudioTracks().some((track) => track.enabled);
  mic.getAudioTracks().forEach((track) => (track.enabled = !muted));
  voiceMute.textContent = muted ? "Unmute mic" : "Mute mic";
  voiceMute.setAttribute("aria-pressed", String(muted));
  if (muted) transcriptPreview.textContent = "Microphone muted.";
  interruptVoice();
});
voiceMute.id = "voice-mute";
voiceMute.hidden = true;
voiceMute.setAttribute("aria-pressed", "false");
const voiceInterrupt = button("Stop reply", interruptVoice);
voiceInterrupt.id = "voice-interrupt";
voiceInterrupt.hidden = true;
const speakerGuard = new JarvisSpeakerGuard();
const speakerProtection = el("input");
speakerProtection.type = "checkbox";
speakerProtection.id = "speaker-protection";
speakerProtection.checked = phoneLayout.matches;
try {
  const saved = localStorage.getItem("jarvis-speaker-protection");
  if (saved !== null) speakerProtection.checked = saved === "true";
} catch {}
const speakerLabel = el("label", "Speaker protection");
speakerLabel.htmlFor = speakerProtection.id;
speakerLabel.prepend(speakerProtection);
const speakerHint = el("small");
function updateSpeakerHint() {
  speakerHint.textContent = speakerProtection.checked
    ? "Mic pauses during playback to prevent feedback. Tap Stop reply to interrupt."
    : "Headphones recommended. Speak to interrupt JARVIS naturally.";
}
speakerProtection.addEventListener("change", () => {
  try {
    localStorage.setItem(
      "jarvis-speaker-protection",
      String(speakerProtection.checked),
    );
  } catch {}
  updateSpeakerHint();
});
updateSpeakerHint();
const speakerControls = el("div", undefined, "speaker-controls");
speakerControls.append(speakerLabel, speakerHint);
const voiceDiagnostics = el("details");
voiceDiagnostics.append(el("summary", "Voice details"));
const voiceTiming = el(
  "small",
  "Audio is processed locally on your Nano. Microphone access ends when you stop voice or lock JARVIS. No actor's voice is cloned.",
);
voiceDiagnostics.append(voiceTiming);
voiceDiagnostics.addEventListener("toggle", async () => {
  if (!voiceDiagnostics.open) return;
  await refreshMicrophones();
  try {
    const status = await api("/health");
    voiceStats.asr = status.voice.stt === "ready" ? "healthy" : "unhealthy";
    voiceStats.tts = status.voice.tts === "ready" ? "healthy" : "unhealthy";
  } catch {
    voiceStats.asr = "unavailable";
  }
});
const voiceLab = el("details");
voiceLab.append(el("summary", "Voice lab · choose JARVIS's voice"));
const labContent = el("div");
voiceLab.append(labContent);
voiceLab.addEventListener("toggle", () => {
  if (voiceLab.open) loadVoiceLab();
});
async function loadVoiceLab() {
  try {
    const data = await api("/voice/lab");
    labContent.replaceChildren(
      el("p", data.reference),
      el(
        "small",
        "Audition scripts are fictional examples, not your actual schedule.",
      ),
    );
    const player = el("audio");
    player.controls = true;
    player.setAttribute("aria-label", "Voice audition playback");
    labContent.append(player);
    for (const candidate of data.profiles) {
      const row = el("section", undefined, "voice-candidate");
      row.append(
        el(
          "h3",
          candidate.label +
            (data.selected === candidate.id ? " · current" : ""),
        ),
      );
      for (const script of Object.keys(data.scripts)) {
        const existing = data.auditions.find(
          (a) => a.candidate === candidate.id && a.script === script,
        );
        row.append(
          button((existing ? "Play " : "Generate ") + script, async (event) => {
            const control = event.currentTarget;
            control.disabled = true;
            try {
              if (mic || voiceStarting) await stopVoice();
              const sample =
                existing ||
                (await api("/voice/lab/generate", {
                  candidate: candidate.id,
                  script,
                }));
              player.pause();
              player.src = sample.audio_path;
              await player.play();
              control.textContent = "Play " + script;
            } catch (error) {
              toast(error.message);
            } finally {
              control.disabled = false;
            }
          }),
        );
      }
      row.append(
        button("Use this voice", async () => {
          if (mic || voiceStarting) await stopVoice();
          player.pause();
          await api("/voice/lab/select", { candidate: candidate.id });
          toast("JARVIS's voice selection is saved.");
          loadVoiceLab();
        }),
      );
      const details = el("details");
      details.append(
        el("summary", "Tuning and provenance"),
        el(
          "pre",
          JSON.stringify(
            {
              ...candidate,
              auditions: data.auditions
                .filter((a) => a.candidate === candidate.id)
                .map((a) => ({
                  script: a.script,
                  generation_ms: a.generation_ms,
                  seed: a.seed,
                  audio_path: a.audio_path,
                })),
            },
            null,
            2,
          ),
        ),
      );
      row.append(details);
      labContent.append(row);
    }
  } catch (error) {
    labContent.replaceChildren(el("p", error.message));
  }
}
const micSelector = el("select");
micSelector.id = "voice-device";
micSelector.setAttribute("aria-label", "Microphone");
const micDefault = el("option", "System default microphone");
micDefault.value = "";
micSelector.append(micDefault);
const inputMeter = el("meter");
inputMeter.id = "voice-level";
inputMeter.min = 0;
inputMeter.max = 0.15;
inputMeter.setAttribute("aria-label", "Live microphone level");
const transcriptPreview = el("p", "Your speech will appear here.");
transcriptPreview.id = "voice-transcript";
transcriptPreview.setAttribute("aria-live", "polite");
const outputVolume = el("input");
outputVolume.type = "range";
outputVolume.min = 0;
outputVolume.max = 1;
outputVolume.step = 0.05;
outputVolume.value = 0.8;
outputVolume.setAttribute("aria-label", "Voice output volume");
outputVolume.oninput = () => {
  if (playbackGain) playbackGain.gain.value = Number(outputVolume.value);
};
const diagnosticOutput = el("pre");
diagnosticOutput.id = "voice-diagnostics";
voiceDiagnostics.append(diagnosticOutput);
const voiceDeviceRow = el("div", undefined, "voice-actions");
voiceDeviceRow.append(
  el("label", "Microphone"),
  micSelector,
  inputMeter,
  el("label", "Volume"),
  outputVolume,
);
const voiceStats = {
  permission: "prompt",
  frames: 0,
  rms: 0,
  websocket: "disconnected",
  asr: "checking",
  speech: false,
  partial: "",
  final: "",
  reconnects: 0,
};
let selectedMic = "";
try {
  selectedMic = localStorage.getItem("jarvis.microphone") || "";
} catch {}
async function refreshMicrophones() {
  try {
    voiceStats.permission = (
      await navigator.permissions.query({ name: "microphone" })
    ).state;
  } catch {
    voiceStats.permission = "unavailable";
  }
  try {
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(
      (d) => d.kind === "audioinput",
    );
    voiceStats.microphones = devices.map(
      (d) => d.label || "Microphone (permission required)",
    );
    micSelector.replaceChildren(micDefault);
    for (const device of devices) {
      const option = el(
        "option",
        device.label || "Microphone (permission required)",
      );
      option.value = device.deviceId;
      micSelector.append(option);
    }
    micSelector.value = selectedMic;
  } catch {
    voiceStats.microphones = [];
  }
}
micSelector.onchange = async () => {
  selectedMic = micSelector.value;
  try {
    localStorage.setItem("jarvis.microphone", selectedMic);
  } catch {}
  const active = Boolean(mic);
  if (active) {
    await stopVoice();
    await startVoice(true);
  }
};
navigator.mediaDevices?.addEventListener("devicechange", refreshMicrophones);
refreshMicrophones();
setInterval(() => {
  inputMeter.value = voiceStats.rms;
  if (
    mic?.getAudioTracks().some((track) => track.enabled) &&
    voiceStats.frames > 500 &&
    voiceStats.peak_rms === 0
  ) {
    voiceHelp.textContent =
      "No sound is reaching JARVIS from this microphone. Check its mute switch or choose another input.";
  }
  diagnosticOutput.textContent = JSON.stringify(
    {
      secure_context: isSecureContext,
      ...voiceStats,
      selected_microphone:
        mic?.getAudioTracks()[0]?.label ||
        micSelector.selectedOptions[0]?.textContent,
      stream_active: mic?.active || false,
      tracks:
        mic?.getAudioTracks().map((t) => ({
          state: t.readyState,
          enabled: t.enabled,
          muted: t.muted,
        })) || [],
      audio_context: audioContext?.state || "closed",
      sample_rate: audioContext?.sampleRate,
      conversation_active: Boolean(mic),
      state: voicePanel.dataset.state,
      playback_active: Boolean(playing),
    },
    null,
    2,
  );
}, 250);
voiceActions.append(voiceButton, voiceSample, voiceMute, voiceInterrupt);
voiceDiagnostics.prepend(voiceDeviceRow);
voicePanel.append(
  voiceStatus,
  voiceHelp,
  voiceActions,
  speakerControls,
  transcriptPreview,
  voiceDiagnostics,
  voiceLab,
);
$("#chat-form").before(voicePanel);
let mic = null,
  playbackGain = null,
  playbackAnalyser = null,
  audioContext = null,
  voiceSocket = null,
  voiceNode = null,
  playing = null,
  audioQueue = [],
  audioEpoch = 0,
  voiceGeneration = 0,
  voiceStarting = false,
  voiceDone = false,
  voiceTurn = null;
const orbButton = button("", async () => {
  if (!phoneLayout.matches) {
    $(".chat-panel").classList.add("conversation-focus");
    orbClose.hidden = false;
  }
  if (!mic && !voiceStarting && !voiceSocket) await startVoice(true);
});
orbButton.id = "jarvis-orb";
orbButton.hidden = true;
orbButton.setAttribute("aria-label", "Talk to JARVIS");
const orbCanvas = el("canvas");
orbCanvas.setAttribute("aria-hidden", "true");
const orbLabel = el("span", "Checking connection", "orb-label");
orbLabel.setAttribute("aria-hidden", "true");
orbButton.append(orbCanvas, orbLabel);
voicePanel.prepend(orbButton);
const orbClose = button("Back to workspace", async () => {
  await stopVoice();
  $(".chat-panel").classList.remove("conversation-focus");
  orbClose.hidden = true;
  orbButton.focus();
});
orbClose.id = "conversation-close";
orbClose.hidden = true;
$(".chat-heading").append(orbClose);
const orbInput = new Float32Array(128),
  orbOutput = new Float32Array(256);
const orb = JarvisOrb.create(
  orbCanvas,
  () => {
    let output = 0;
    if (playing && playbackAnalyser) {
      playbackAnalyser.getFloatTimeDomainData(orbOutput);
      output = Math.sqrt(
        orbOutput.reduce((sum, n) => sum + n * n, 0) / orbOutput.length,
      );
    }
    return {
      ...orbStatus,
      voice: voicePanel.dataset.state || "off",
      input: mic?.getAudioTracks().some((t) => t.enabled) ? voiceStats.rms : 0,
      output,
      samples: playing ? orbOutput : orbInput,
    };
  },
  (state, snapshot) => {
    orbLabel.textContent = {
      idle: "Talk to JARVIS",
      offline: "Connection unavailable",
      degraded: "Needs attention",
      listening: "Listening",
      hearing: "Hearing you",
      interrupted: "Listening again",
      speaking: "Speaking",
      thinking: "Working on your request",
      muted: "Microphone muted",
      tool: snapshot.activity || "Reading saved work",
      attention: "Overdue commitment to review",
    }[state];
  },
);
let orbHealthPending = false;
async function refreshOrbHealth() {
  if (orbHealthPending || document.hidden || $("#workspace").hidden) return;
  orbHealthPending = true;
  try {
    const status = await api("/health");
    orbStatus.online = true;
    orbStatus.ready =
      status.database &&
      status.models.primary === "ready" &&
      status.voice.stt === "ready" &&
      status.voice.tts === "ready";
    orbStatus.enabled = status.interface?.orb === true;
    responseEvidenceEnabled = status.interface?.response_evidence === true;
    document.querySelectorAll(".response-evidence").forEach((button) => {
      button.hidden = !responseEvidenceEnabled;
    });
    orbButton.hidden = !orbStatus.enabled;
    voicePanel.classList.toggle("with-orb", orbStatus.enabled);
  } catch {
    orbStatus.online = false;
  } finally {
    orbHealthPending = false;
  }
}
setInterval(refreshOrbHealth, 30000);
window.addEventListener("offline", () => {
  orbStatus.online = false;
});
window.addEventListener("online", refreshOrbHealth);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshOrbHealth();
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "Escape" &&
    $(".chat-panel").classList.contains("conversation-focus") &&
    !$("#detail").open
  )
    orbClose.click();
});
window.addEventListener("pagehide", (event) => {
  if (!event.persisted) orb.destroy();
});
function voiceState(state) {
  voicePanel.dataset.state = state;
  if (state !== "thinking") orbStatus.activity = "";
  const labels = {
    off: "Voice is off",
    connecting: "Connecting…",
    hearing: "Hearing you…",
    interrupted: "Interrupted — hearing you…",
    reconnecting: "Reconnecting…",
    listening: "Listening to you",
    muted: "Microphone muted",
    transcribing: "Understanding what you said…",
    thinking: "Checking your context…",
    synthesizing: "Preparing his voice…",
    speaking: "JARVIS is speaking",
    error: "Voice needs attention",
  };
  voiceStatus.textContent = labels[state] || state;
  voiceInterrupt.hidden = ![
    "transcribing",
    "thinking",
    "synthesizing",
    "speaking",
  ].includes(state);
}
function idleVoice() {
  voiceHelp.textContent =
    "Speak naturally. Pause to let JARVIS answer. End Conversation releases your microphone.";
  if (!mic) {
    if (voiceSocket) stopVoice();
    return;
  }
  voiceState(
    mic.getAudioTracks().some((track) => track.enabled) ? "listening" : "muted",
  );
}
function cancelAudio() {
  audioEpoch++;
  audioQueue = [];
  const old = playing;
  playing = null;
  if (old) {
    speakerGuard.ended(performance.now());
    old.onended = null;
    try {
      old.stop();
    } catch {}
    old.disconnect();
  }
  voiceStats.playback_stopped_at = performance.now();
}
function interruptVoice() {
  cancelAudio();
  voiceTurn = null;
  voiceDone = true;
  if (voiceSocket?.readyState === 1) voiceSocket.send("cancel");
  idleVoice();
}
function playNext() {
  if (playing || !audioQueue.length || !audioContext) return;
  const { buffer, turn } = audioQueue.shift();
  const source = audioContext.createBufferSource();
  playing = source;
  source.buffer = buffer;
  source.connect(playbackGain || audioContext.destination);
  source.onended = () => {
    if (playing !== source) return;
    source.disconnect();
    playing = null;
    speakerGuard.ended(performance.now());
    if (voiceSocket?.readyState === 1)
      voiceSocket.send("playback-ended:" + turn);
    if (audioQueue.length) playNext();
    else if (voiceDone) idleVoice();
    else voiceState("synthesizing");
  };
  source.start();
  if (voiceStats.playback_turn !== turn) {
    voiceStats.playback_started_at = performance.now();
    voiceStats.playback_turn = turn;
  }
  voiceState("speaking");
  if (voiceSocket?.readyState === 1) voiceSocket.send("playback:" + turn);
}
async function stopVoice() {
  voiceGeneration++;
  voiceStarting = false;
  const stream = mic,
    context = audioContext,
    socket = voiceSocket;
  mic = audioContext = voiceSocket = null;
  stream?.getTracks().forEach((track) => track.stop());
  voiceNode?.disconnect();
  voiceNode = null;
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
  cancelAudio();
  voiceTurn = null;
  voiceButton.disabled = false;
  voiceButton.textContent = "Start Conversation";
  voiceButton.setAttribute("aria-pressed", "false");
  voiceSample.disabled = false;
  voiceMute.hidden = true;
  voiceMute.textContent = "Mute mic";
  voiceMute.setAttribute("aria-pressed", "false");
  voiceState("off");
  voiceStats.websocket = "disconnected";
  voiceStats.rms = 0;
  voiceStats.speech = false;
  playbackGain = null;
  playbackAnalyser = null;
  if (context && context.state !== "closed") await context.close();
}
async function toggleVoice() {
  if (voiceSocket || mic || voiceStarting) await stopVoice();
  else await startVoice(true);
}
async function startVoice(withMic, reconnecting = false) {
  if (voiceSocket || voiceStarting) return;
  if (!reconnecting) voiceStats.reconnects = 0;
  voiceLab.querySelector("audio")?.pause();
  const generation = ++voiceGeneration;
  voiceStarting = true;
  voiceDone = false;
  voiceButton.disabled = false;
  voiceButton.textContent = "End Conversation";
  voiceButton.setAttribute("aria-pressed", "true");
  voiceSample.disabled = true;
  voiceState("connecting");
  try {
    if (withMic && !navigator.mediaDevices?.getUserMedia)
      throw new Error(
        "Microphone access requires opening JARVIS through the Windows launcher or a secure connection.",
      );
    let context;
    try {
      context = new AudioContext({ sampleRate: 16000 });
    } catch (error) {
      if (error.name !== "NotSupportedError") throw error;
      context = new AudioContext(); // The worklet resamples device-native rates to 16 kHz.
    }
    audioContext = context;
    await context.resume();
    context.onstatechange = () => {
      if (
        phoneLayout.matches &&
        audioContext === context &&
        ["interrupted", "suspended"].includes(context.state)
      ) {
        stopVoice();
        voiceHelp.textContent =
          "Your phone interrupted audio. Tap Start Conversation when you’re ready.";
      }
    };
    playbackGain = context.createGain();
    playbackGain.gain.value = Number(outputVolume.value);
    playbackAnalyser = context.createAnalyser();
    playbackAnalyser.fftSize = 256;
    playbackGain.connect(playbackAnalyser);
    playbackAnalyser.connect(context.destination);
    if (withMic) {
      await context.audioWorklet.addModule("/voice-worklet.js");
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          ...(selectedMic ? { deviceId: { exact: selectedMic } } : {}),
        },
      });
      if (generation !== voiceGeneration) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      mic = stream;
      voiceHelp.textContent =
        "Speak naturally. Pause to let JARVIS answer; speak again to interrupt. End Conversation releases your microphone.";
      voiceStats.frames = 0;
      voiceStats.peak_rms = 0;
      await refreshMicrophones();
      stream.getAudioTracks().forEach((track) => {
        track.onended = () => {
          stopVoice();
          voiceState("error");
          voiceHelp.textContent =
            "Microphone disconnected. Select an available microphone and start again.";
        };
      });
    }
    if (generation !== voiceGeneration) return;
    const socket = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/api/voice",
    );
    voiceSocket = socket;
    voiceStats.websocket = "connecting";
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () =>
          reject(
            new Error(
              "The voice connection timed out. Check System Health, then try again.",
            ),
          ),
        12000,
      );
      socket.onopen = () => {
        clearTimeout(timeout);
        resolve();
      };
      socket.onerror = socket.onclose = () => {
        clearTimeout(timeout);
        reject(
          new Error(
            "The voice connection closed. Unlock JARVIS and try again.",
          ),
        );
      };
    });
    if (generation !== voiceGeneration) return;
    voiceStats.websocket = "connected";
    socket.send(
      JSON.stringify({
        type: "client_diagnostics",
        permission: voiceStats.permission,
        device: mic?.getAudioTracks()[0]?.label || "playback only",
        secure_context: isSecureContext,
        sample_rate: context.sampleRate,
      }),
    );
    let decodeChain = Promise.resolve();
    socket.onmessage = (event) => {
      if (generation !== voiceGeneration) return;
      const m = JSON.parse(event.data);
      if (m.type === "hello") {
        voiceStats.asr = m.asr;
        voiceStats.session_id = m.session_id;
      }
      if (m.type === "speech") {
        voiceStats.speech = m.active;
        if (m.active) {
          voiceStats.speech_started_at = performance.now();
          voiceStats.partial = "";
          transcriptPreview.textContent = "Hearing you…";
          cancelAudio();
          voiceState("hearing");
        }
      }
      if (m.type === "partial") {
        voiceStats.partial = m.text;
        transcriptPreview.textContent = m.text;
      }
      if (m.type === "activity") orbStatus.activity = m.active ? m.label : "";
      if (m.type === "cancel") {
        voiceStats.speech = false;
        cancelAudio();
        voiceTurn = null;
        voiceDone = false;
        if (mic && !voiceStats.speech) idleVoice();
        return;
      }
      if (m.type === "state") {
        voiceTurn = m.turn_id;
        voiceDone = false;
        voiceStats.backend_state = m.state;
        if (!(playing && m.state === "synthesizing")) voiceState(m.state);
      }
      if (m.type === "transcript") {
        voiceStats.final_transcript_at = performance.now();
        voiceStats.final = m.text;
        voiceStats.partial = "";
        transcriptPreview.textContent = m.text;
        message("user", m.text);
      }
      if (m.type === "reply")
        message("assistant", m.text, m.sources || [], m.message_id);
      if (m.type === "progress" && m.turn_id === voiceTurn)
        voiceHelp.textContent = m.text;
      if (m.type === "error") {
        cancelAudio();
        voiceDone = true;
        voiceState("error");
        toast(m.text);
        voiceHelp.textContent = m.text;
        return;
      }
      if (m.type === "empty") {
        voiceDone = true;
        idleVoice();
      }
      if (m.type === "audio") {
        const epoch = audioEpoch;
        decodeChain = decodeChain
          .then(async () => {
            if (generation !== voiceGeneration || epoch !== audioEpoch) return;
            const bytes = Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0));
            const buffer = await context.decodeAudioData(bytes.buffer);
            if (
              generation !== voiceGeneration ||
              epoch !== audioEpoch ||
              m.turn_id !== voiceTurn
            )
              return;
            audioQueue.push({ buffer, turn: m.turn_id });
            playNext();
          })
          .catch(() => {
            if (generation === voiceGeneration) {
              cancelAudio();
              voiceState("error");
              toast("Audio playback failed. Stop voice and try again.");
            }
          });
      }
      if (m.type === "done") {
        decodeChain.then(() => {
          if (generation !== voiceGeneration || m.turn_id !== voiceTurn) return;
          voiceDone = true;
          if (m.timings)
            voiceTiming.textContent = `This turn: understood in ${(m.timings.stt_complete / 1000).toFixed(1)}s; first audio ready in ${(m.timings.first_audio / 1000).toFixed(1)}s. Processed locally on Nano.`;
          if (!playing && !audioQueue.length) idleVoice();
        });
      }
    };
    socket.onclose = async (event) => {
      if (generation !== voiceGeneration) return;
      const resume =
        withMic && event.code !== 1008 && voiceStats.reconnects < 3;
      await stopVoice();
      if (resume) {
        const stoppedGeneration = voiceGeneration;
        voiceStats.reconnects++;
        voiceStarting = true;
        voiceButton.textContent = "End Conversation";
        voiceState("reconnecting");
        voiceHelp.textContent =
          "Connection interrupted. Your conversation is saved. Reconnecting…";
        setTimeout(() => {
          if (voiceGeneration === stoppedGeneration) {
            voiceStarting = false;
            startVoice(true, true);
          }
        }, 1000 * voiceStats.reconnects);
      } else {
        voiceState("error");
        voiceHelp.textContent =
          event.code === 1008
            ? "Your private session expired. Sign in again; on your phone, use a fresh pairing code from JARVIS on Windows."
            : "Voice disconnected. Check the Nano connection, then start again.";
      }
    };
    if (withMic) {
      const source = context.createMediaStreamSource(mic);
      voiceNode = new AudioWorkletNode(context, "pcm-recorder");
      voiceNode.port.onmessage = (e) => {
        const samples = speakerGuard.filter(
          new Int16Array(e.data),
          speakerProtection.checked,
          Boolean(playing),
          performance.now(),
        );
        if (orbStatus.enabled)
          for (let i = 0; i < orbInput.length; i++)
            orbInput[i] =
              samples[Math.floor((i * samples.length) / orbInput.length)] /
              32768;
        voiceStats.rms = Math.sqrt(
          samples.reduce((sum, v) => sum + (v / 32768) ** 2, 0) /
            samples.length,
        );
        voiceStats.peak_rms = Math.max(voiceStats.peak_rms, voiceStats.rms);
        if (socket.readyState === 1 && socket.bufferedAmount < 64000) {
          socket.send(e.data);
          voiceStats.frames++;
        }
      };
      source.connect(voiceNode);
      const muted = context.createGain();
      muted.gain.value = 0;
      voiceNode.connect(muted);
      muted.connect(context.destination);
      voiceMute.hidden = false;
      voiceState("listening");
    } else socket.send("preview");
    voiceStarting = false;
    voiceButton.disabled = false;
    voiceButton.textContent = "End Conversation";
    voiceButton.setAttribute("aria-pressed", "true");
  } catch (error) {
    if (generation !== voiceGeneration) return;
    await stopVoice();
    if (
      reconnecting &&
      voiceStats.reconnects < 3 &&
      ![
        "NotAllowedError",
        "NotFoundError",
        "NotReadableError",
        "OverconstrainedError",
      ].includes(error.name)
    ) {
      const stoppedGeneration = voiceGeneration;
      voiceStats.reconnects++;
      voiceStarting = true;
      voiceButton.textContent = "End Conversation";
      voiceState("reconnecting");
      setTimeout(() => {
        if (voiceGeneration === stoppedGeneration) {
          voiceStarting = false;
          startVoice(true, true);
        }
      }, 1000 * voiceStats.reconnects);
      return;
    }
    voiceState("error");
    const explanation =
      error.name === "NotAllowedError"
        ? "Microphone permission is blocked. Click the microphone icon in Chrome's address bar and allow access, then select Start Conversation."
        : error.name === "NotFoundError"
          ? "No microphone was found. Connect a microphone or headset, then try again."
          : error.message ||
            "Local voice is unavailable. Text remains available.";
    voiceHelp.textContent = explanation;
    toast(explanation);
  }
}
window.addEventListener("pagehide", () => stopVoice());
if (document.modelContext?.registerTool) {
  const lifecycle = new AbortController();
  window.addEventListener("pagehide", () => lifecycle.abort(), { once: true });
  Promise.resolve(
    document.modelContext.registerTool(
      {
        name: "read_workday",
        description:
          "Read the same source-backed workday data displayed on Today. Does not approve or execute any action.",
        inputSchema: {
          type: "object",
          properties: {},
          additionalProperties: false,
        },
        annotations: { readOnlyHint: true, untrustedContentHint: true },
        execute: (input) => {
          if (!input || Object.keys(input).length)
            throw Error("No parameters expected");
          return api("/today");
        },
      },
      { signal: lifecycle.signal },
    ),
  ).catch(() => {});
}
