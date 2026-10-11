"use strict";
let setupFocus = null;
const setupGuides = {
  outlook: {
    name: "Microsoft Outlook",
    number: "01",
    purpose: "Your HP workday, with context.",
    capability:
      "Read your HP email, including Sent and attachments, and your calendar. JARVIS can prepare internal drafts and meeting briefs. Initial access does not send email or change meetings. Those actions require additional Microsoft consent and your specific approval.",
    steps: [
      "Ask HP IT for an approved Microsoft Entra application's client ID and tenant ID. These are identifiers, not passwords.",
      "Save the two IDs, then sign in directly with Microsoft and complete HP MFA or consent.",
      "Test read access, then enable Outlook. JARVIS will begin its existing 90-day sync in the background.",
    ],
    fields: [
      ["ms_client_id", "Application (client) ID", "text", "Provided by HP IT"],
      ["ms_tenant_id", "Directory (tenant) ID", "text", "Provided by HP IT"],
    ],
    help: "If consent is denied, HP IT must approve access. If HP blocks device-code sign-in, the Windows launcher supports normal Microsoft browser sign-in. Never enter your HP password here or bypass a tenant restriction.",
    revoke:
      "Disconnect removes JARVIS's local Microsoft sign-in and disables syncing. It keeps existing work records. To revoke the app's permission at Microsoft, use your Microsoft account's app permissions or ask HP IT.",
    link: [
      "Review Microsoft app access",
      "https://myapplications.microsoft.com/",
    ],
  },
  telegram: {
    name: "Telegram",
    number: "02",
    purpose: "One private line to JARVIS.",
    capability:
      "Exchange messages, receive briefings and review specific approvals in your private chat. Only your configured numeric user ID is authorized. Group chats and other users are rejected. Enabling this connection starts polling and permits notifications to you.",
    steps: [
      "Open Telegram's official BotFather and create a dedicated JARVIS bot with /newbot. Copy its bot token.",
      "Open a private chat with your new bot and press Start. Enter your own numeric Telegram user ID, not your username or the bot's ID.",
      "Save securely and test the bot and private chat. Enable Telegram when you are ready for messages. The connection test sends no message.",
    ],
    fields: [
      [
        "telegram_token",
        "Bot token",
        "password",
        "Paste once; never shown again",
      ],
      [
        "telegram_user_id",
        "Your numeric Telegram user ID",
        "text",
        "Positive number, for example 123456789",
      ],
    ],
    help: "If the private chat cannot be verified, press Start in your bot's chat and check your numeric user ID. Your Telegram data export can show your user ID. A bot with an existing webhook needs its previous integration reviewed; JARVIS will not remove it silently.",
    revoke:
      "Disconnect clears the saved token and stops JARVIS polling and notifications. Revoke or rotate the bot token through BotFather to invalidate it at Telegram.",
    link: ["Open official BotFather", "https://t.me/BotFather"],
  },
  twilio: {
    name: "Twilio",
    number: "03",
    purpose: "A familiar voice when it matters.",
    capability:
      "Call only your configured number, play a briefing and carry on a voice conversation. Calls can incur Twilio charges. Consequential actions still need approval in Telegram or the dashboard. Incoming telephone calls are not enabled.",
    steps: [
      "Sign in to Twilio and choose a dedicated, voice-capable number that belongs to your account. Complete any required billing step there.",
      "Enter the account SID, auth token, JARVIS number and your destination number. Use +country-code format.",
      "Authorize an HTTPS/WSS ingress that forwards only to the private phone gateway. Enter its public base address below; Advanced Details lists the exact boundary.",
      "Test the account and number, then enable the phone gateway. Finally request a test call, answer it and ask JARVIS a short question. Telephone readiness requires a real response to play.",
    ],
    fields: [
      [
        "twilio_account_sid",
        "Twilio account SID",
        "password",
        "AC followed by 32 characters",
      ],
      [
        "twilio_auth_token",
        "Twilio auth token",
        "password",
        "Paste once; never shown again",
      ],
      ["twilio_from", "Dedicated JARVIS phone number", "tel", "+1505…"],
      ["demo_phone", "Your phone number", "tel", "+1505…"],
      [
        "public_webhook_url",
        "Public phone gateway address",
        "url",
        "https://your-authorized-phone-gateway.example",
      ],
    ],
    help: "Account test checks credentials and ownership of the voice-capable number. It does not purchase a number, change Twilio settings, expose a service or place a call. For trial accounts, Twilio may require your destination number to be verified. Failed calls usually need billing, routing or ingress attention.",
    revoke:
      "Disconnect removes local Twilio credentials and stops the private phone gateway. To revoke the credential at Twilio, rotate its auth token in the Twilio console. Your number and billing remain unchanged.",
    link: ["Open Twilio console", "https://console.twilio.com/"],
  },
};

function setupPill(text, state) {
  return el(
    "span",
    text,
    "setup-pill " +
      (state === "Ready" ? "good" : state === "Offline" ? "bad" : "pending"),
  );
}
function setupDetails(title, ...contents) {
  const details = el("details", undefined, "setup-details");
  details.append(el("summary", title), ...contents);
  return details;
}
function externalLink(label, url) {
  const link = el("a", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
function setupConfirm(title, description, actionLabel, action) {
  const dialog = $("#detail");
  $("#detail-title").textContent = title;
  const content = $("#detail-body");
  content.replaceChildren(el("p", description));
  const controls = el("div", undefined, "setup-actions");
  controls.append(
    button("Cancel", () => dialog.close()),
    button(
      actionLabel,
      async () => {
        await action();
        dialog.close();
        await render();
      },
      "primary",
    ),
  );
  content.append(controls);
  dialog.showModal();
}

async function renderAccountSetup() {
  const fragment = document.createDocumentFragment();
  fragment.append(
    intro(
      "At your service. On your terms.",
      "Connect your accounts when you are ready. Your local assistant is already here; each connection has its own permissions and a clear off switch.",
    ),
  );
  let data;
  try {
    data = await api("/setup");
  } catch (error) {
    const problem = el("section", undefined, "setup-card");
    problem.append(
      el("h2", "JARVIS Core"),
      setupPill("Offline", "Offline"),
      el(
        "p",
        "The private connection could not be checked. Reopen JARVIS from the Windows launcher and confirm the Nano is online.",
      ),
      button("Try again", render),
    );
    fragment.append(problem);
    return fragment;
  }
  const overview = el("div", undefined, "setup-overview");
  const core = el("section", undefined, "setup-card local-card");
  core.append(
    el("span", "YOUR PRIVATE WORKSPACE", "eyebrow"),
    el("h2", "JARVIS Core"),
    setupPill(data.core.status, data.core.state),
    el(
      "p",
      "Your assistant, approvals and activity record are available through this private connection.",
    ),
  );
  const ai = el("section", undefined, "setup-card local-card");
  ai.append(
    el("span", "INTELLIGENCE ON YOUR NANO", "eyebrow"),
    el("h2", "Local AI"),
    setupPill(
      data.local_ai.status,
      data.local_ai.status === "Connected" ? "Ready" : "Degraded",
    ),
    el("p", data.local_ai.model, "model-name"),
    el(
      "small",
      `Inference endpoint: ${data.local_ai.status === "Connected" ? "Responding" : "Needs attention"} · Memory: ${data.local_ai.memory}`,
    ),
    el(
      "small",
      `Voice input: ${data.local_ai.voice_input === "ready" ? "Available" : "Unavailable"} · Voice output: ${data.local_ai.voice_output === "ready" ? "Available" : "Unavailable"}`,
    ),
  );
  overview.append(core, ai);
  fragment.append(overview);
  fragment.append(
    heading("Your connections", "Saving details does not enable an account."),
  );
  for (const [provider, baseGuide] of Object.entries(setupGuides)) {
    const state = data.integrations[provider];
    const browserMode =
      provider === "outlook" && state.connection_method === "browser";
    const guide = browserMode
      ? {
          ...baseGuide,
          fields: [],
          capability:
            "Read recent Inbox and Sent previews, selected full message bodies, and a bounded calendar window through your signed-in Nano browser. Background sync runs every five minutes. Sending, read flags, read receipts, drafts in Outlook and calendar changes are blocked. Internal JARVIS drafts remain available. Full 90-day history, all attachments and complete meeting attendees are not yet indexed.",
          steps: [
            "Keep the Nano browser signed into your HP Outlook account. No Entra application IDs are needed for this mode.",
            "Test Inbox, Sent and calendar access, then enable the read-only connector.",
            "If HP requests sign-in again, reopen the Nano Outlook viewer and complete normal MFA. Windows does not need to remain open for sync.",
          ],
          help: "Open the Nano Outlook viewer with scripts/open_nano_outlook.ps1 if sign-in expires. A successful connection does not mean the entire mailbox is indexed. Check the coverage information below.",
          revoke:
            "Disconnect disables JARVIS browser synchronization and retains work records. The browser remains signed in for you. Sign out inside Outlook to revoke that session; JARVIS does not silently delete its profile.",
          link: ["Outlook Web", "https://outlook.office.com/mail/"],
        }
      : baseGuide;
    const card = el("section", undefined, "setup-card integration-card");
    card.dataset.integration = provider;
    const top = el("div", undefined, "setup-card-header");
    const title = el("div");
    title.append(
      el("span", guide.number + " / CONNECTION", "eyebrow"),
      el("h2", guide.name),
      el("p", guide.purpose),
    );
    top.append(title, setupPill(state.status, state.state));
    card.append(top);
    card.append(
      el("p", state.detail, "connection-detail"),
      el(
        "small",
        "Last successful connection: " +
          (state.last_success ? time(state.last_success) : "Not yet verified"),
      ),
    );
    const actions = el("div", undefined, "setup-actions");
    if (provider === "outlook" && !browserMode) {
      actions.append(
        button("Use signed-in Nano browser", () =>
          setupConfirm(
            "Use your Nano Outlook browser?",
            "Select the existing browser session for read-only access. This requires normal HP sign-in, but no Entra app IDs. Sync remains disabled until you test and enable it.",
            "Select browser connection",
            async () => {
              await api("/outlook/browser/select", { confirm: true });
              toast("Browser selected. Test access, then enable it.");
            },
          ),
        ),
      );
    }
    if (browserMode) {
      card.append(
        el("small", "Connection method: Nano Outlook browser · Read only"),
      );
      if (state.coverage) {
        card.append(el("p", state.coverage.scope, "help"));
        const indexed = state.coverage.indexed;
        if (indexed)
          card.append(
            el(
              "p",
              `${indexed.previews} conversation previews · ${indexed.messages} full message bodies · ${indexed.events} calendar entries · ${indexed.attachments || 0} attachments stored locally`,
              "help",
            ),
          );
        for (const [role, progress] of Object.entries(
          state.coverage.history || {},
        )) {
          const folder = role === "inbox" ? "Inbox" : "Sent";
          const oldest = progress.oldest_preview_at
            ? new Date(progress.oldest_preview_at).toLocaleDateString()
            : "not yet verified";
          card.append(
            el(
              "small",
              `${folder}: oldest preview ${oldest}. ${progress.boundary_seen ? "90-day date boundary reached; complete message coverage is still pending." : "History import is still in progress."}`,
            ),
          );
        }
      }
      if (state.enabled)
        actions.append(
          button("Sync now", async () => {
            await api("/outlook/sync", {});
            toast("Read-only synchronization finished.");
            await render();
          }),
        );
    }
    const wizard = setupDetails(
      state.configured ? "Review connection setup" : "Set up " + guide.name,
    );
    wizard.open = setupFocus === provider;
    wizard.ontoggle = () => {
      if (wizard.open) setupFocus = provider;
    };
    const steps = el("ol", undefined, "setup-steps");
    guide.steps.forEach((step) => steps.append(el("li", step)));
    wizard.append(steps, externalLink(...guide.link));
    const form = el("form", undefined, "account-form");
    form.autocomplete = "off";
    const fields = {};
    for (const [name, label, type, placeholder] of guide.fields) {
      const group = el("div", undefined, "setup-field");
      const id = "setup-" + name;
      const input = el("input");
      input.id = id;
      input.type = type;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.maxLength = 1024;
      input.required = !state.fields_saved[name];
      input.placeholder = state.fields_saved[name]
        ? "Saved securely. Leave blank to keep."
        : placeholder;
      const caption = el("label", label);
      caption.htmlFor = id;
      group.append(caption, input);
      form.append(group);
      fields[name] = input;
    }
    const consent = el("label", undefined, "setup-consent");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.required = true;
    consent.append(
      checkbox,
      el(
        "span",
        "Save these account details privately on my Nano. Keep this connection disabled until I enable it.",
      ),
    );
    const save = el("button", "Save securely", "primary");
    save.type = "submit";
    const feedback = el("p", undefined, "setup-feedback");
    feedback.setAttribute("role", "status");
    form.append(
      consent,
      el(
        "small",
        "Credentials are encrypted on the Nano. They are never returned to this page, stored in browser storage or written to ordinary configuration files.",
      ),
      save,
      feedback,
    );
    form.onsubmit = async (event) => {
      event.preventDefault();
      save.disabled = true;
      const values = Object.fromEntries(
        Object.entries(fields).map(([key, input]) => [key, input.value]),
      );
      Object.values(fields).forEach((input) => {
        if (input.type === "password") input.value = "";
      });
      try {
        const result = await api(`/setup/${provider}/configure`, {
          confirm: true,
          values,
        });
        toast(result.message);
        await render();
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        Object.keys(values).forEach((key) => {
          values[key] = "";
        });
        save.disabled = false;
      }
    };
    if (!browserMode) wizard.append(form);
    if (provider === "outlook" && state.configured && !browserMode) {
      wizard.append(
        button("Sign in with Microsoft", async () => {
          const flow = await api("/outlook/connect", {});
          $("#detail-title").textContent = "Complete your HP sign-in";
          $("#detail-body").replaceChildren(
            el(
              "p",
              "Open Microsoft's sign-in page and enter this temporary code. Complete your normal HP sign-in, MFA and consent.",
            ),
            el("strong", flow.user_code, "sign-in-code"),
            externalLink(
              "Open Microsoft sign-in",
              "https://microsoft.com/devicelogin",
            ),
            el(
              "p",
              "Return here after signing in. JARVIS never sees your HP password.",
            ),
            button(
              "Check sign-in",
              async () => {
                const result = await api("/outlook/complete", {});
                if (result.connected) {
                  $("#detail").close();
                  toast("Signed in. Test the connection, then enable Outlook.");
                  await render();
                } else toast(result.message);
              },
              "primary",
            ),
          );
          $("#detail").showModal();
        }),
      );
      wizard.append(
        el(
          "p",
          "If your tenant requires browser-based OAuth, launch JARVIS with the Microsoft sign-in option described in Advanced Details. HP policies remain in control.",
          "help",
        ),
      );
    }
    actions.append(
      button("Test connection", async () => {
        const result = await api(`/setup/${provider}/test`, {});
        toast(result.detail);
        setupFocus = provider;
        await render();
      }),
    );
    if (state.verified && !state.enabled)
      actions.append(
        button(
          "Enable " + guide.name,
          () =>
            setupConfirm(
              "Enable " + guide.name + "?",
              guide.capability,
              "Enable connection",
              async () => {
                await api(`/setup/${provider}/enable`, { confirm: true });
                toast("Connection enabled.");
              },
            ),
          "primary",
        ),
      );
    if (provider === "twilio") {
      const call = button("Place a test call", () =>
        setupConfirm(
          "Call your configured number?",
          "JARVIS will call only your saved destination number. Twilio charges may apply. Answer, ask a short question and listen for JARVIS's reply. Your quiet-hours rule still applies.",
          "Call me now",
          async () => {
            const result = await api("/setup/twilio/test-call", {
              confirm: true,
            });
            toast(result.message);
          },
        ),
      );
      call.disabled = !(state.verified && state.enabled);
      actions.append(call);
      card.append(
        el(
          "small",
          data.phone_verified
            ? "Live phone conversation verified: " + time(data.phone_verified)
            : "Telephone workflow: not yet verified with a real call.",
        ),
      );
    }
    if (browserMode || Object.values(state.fields_saved).some(Boolean))
      actions.append(
        button("Disconnect", () =>
          setupConfirm(
            "Disconnect " + guide.name + "?",
            guide.revoke,
            "Disconnect locally",
            async () => {
              const result = await api(`/setup/${provider}/disconnect`, {
                confirm: true,
              });
              toast(result.message);
            },
          ),
        ),
      );
    card.append(
      actions,
      wizard,
      setupDetails("Permissions & capabilities", el("p", guide.capability)),
      setupDetails(
        "Troubleshooting & revoking access",
        el("p", guide.help),
        el("p", guide.revoke),
      ),
    );
    fragment.append(card);
  }
  const readiness = el("section", undefined, "setup-card readiness-card");
  readiness.id = "system-readiness";
  const readyCount = data.readiness.filter(
    (row) => row.state === "Ready",
  ).length;
  readiness.append(
    el("span", "THE WHOLE PICTURE", "eyebrow"),
    el("h2", "System Readiness"),
    el(
      "p",
      `${readyCount} of ${data.readiness.length} checks ready. An unconnected account is a setup step, not a successful connection.`,
    ),
  );
  data.readiness.forEach((item) => {
    const row = el("div", undefined, "readiness-row");
    const name = el("div");
    name.append(el("strong", item.name), el("small", item.detail));
    row.append(name, setupPill(item.state, item.state));
    readiness.append(row);
  });
  readiness.append(button("Check system readiness", render));
  const legend = setupDetails(
    "What the readiness labels mean",
    el(
      "p",
      "Ready: the relevant check passed. Configuration required: required settings are missing. Authorization required: sign-in, verification or your explicit enable step is pending. Degraded: part of the workflow is available but incomplete, stale or failing. Offline: the service could not be reached.",
    ),
  );
  readiness.append(legend);
  fragment.append(readiness);
  const advanced = setupDetails("Advanced Details");
  advanced.id = "setup-advanced";
  advanced.append(
    el(
      "p",
      "Diagnostics are read-only and exclude credentials and provider response bodies. The event log shows setup actions and safe outcome labels.",
    ),
    el("pre", JSON.stringify(data.advanced, null, 2)),
    el("h3", "Microsoft browser sign-in"),
    el(
      "p",
      "From Windows: scripts/open_jarvis.ps1 -MicrosoftSignIn. This uses the existing approved Entra application and normal Microsoft browser authentication.",
    ),
    el("h3", "Phone ingress boundary"),
    el(
      "p",
      "Forward authorized HTTPS and WSS ingress only to Nano 127.0.0.1:8190. Routes: /twilio/answer/*, /twilio/status/*, /twilio/audio/* and /twilio/media/*. Never forward dashboard 3100, API 8100, model ports or PostgreSQL. Enabling the gateway starts only its private service; public ingress requires your provider authorization.",
    ),
    el(
      "p",
      "Phone readiness is established by a provider-signed live media session acknowledging a JARVIS response. A saved URL or passing credential check cannot establish that. No ingress is provisioned automatically.",
    ),
  );
  fragment.append(advanced);
  return fragment;
}
