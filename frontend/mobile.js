"use strict";
// Reuse the single conversation, microphone and saved-context renderer on every screen.
globalThis.JarvisMobile = (() => {
  const small = phoneLayout;
  const nav = el("nav", undefined, "phone-nav");
  nav.setAttribute("aria-label", "Phone navigation");
  for (const [name, title, glyph] of [
    ["Voice", "Voice", "◉"],
    ["Second Brain", "Brain", "◈"],
    ["Today", "Today", "◷"],
    ["More", "More", "☰"],
  ]) {
    const n = button("", () => {
      if (name === "More") {
        document.body.dataset.phoneView = "more";
        nav
          .querySelectorAll("button")
          .forEach((b) =>
            b.setAttribute("aria-current", b === n ? "page" : "false"),
          );
      } else navigate(name);
      window.scrollTo(0, 0);
    });
    n.dataset.phone = name;
    n.append(el("span", glyph), el("small", title));
    nav.append(n);
  }
  $("#workspace").append(nav);
  const live = button(
    "Voice is active · Return to conversation",
    () => navigate("Voice"),
    "phone-live",
  );
  live.hidden = true;
  $("#workspace").append(live);
  const banner = el(
    "p",
    "Keep JARVIS open while talking. Audio pauses when you leave the app or lock your phone.",
    "phone-hint",
  );
  function arrangeVoiceSettings() {
    voiceDiagnostics.append(voiceLab, banner);
    if (small.matches)
      voicePanel.insertBefore(speakerControls, transcriptPreview);
    else voiceDiagnostics.append(speakerControls);
    voiceDiagnostics.querySelector("summary").textContent = small.matches
      ? "Voice settings"
      : "Voice details";
    $("#chat-input").rows = small.matches ? 1 : 2;
  }
  arrangeVoiceSettings();
  let wake = null,
    wakePending = false;
  async function releaseWake() {
    const held = wake;
    wake = null;
    await held?.release().catch(() => {});
  }
  async function holdWake() {
    if (
      !small.matches ||
      !mic ||
      document.hidden ||
      wake ||
      wakePending ||
      !navigator.wakeLock
    )
      return;
    wakePending = true;
    try {
      wake = await navigator.wakeLock.request("screen");
      if (!mic || document.hidden) await releaseWake();
    } catch {
      /* Optional capability; voice works without a screen wake lock. */
    } finally {
      wakePending = false;
    }
  }
  new MutationObserver(() => {
    live.hidden = !mic;
    if (mic) holdWake();
    else releaseWake();
  }).observe(voicePanel, { attributes: true, attributeFilter: ["data-state"] });
  document.addEventListener("visibilitychange", async () => {
    if (
      small.matches &&
      document.hidden &&
      (mic || voiceStarting || voiceSocket)
    ) {
      await stopVoice();
      voiceHelp.textContent =
        "Conversation paused while you were away. Tap Start Conversation to resume.";
      await releaseWake();
    }
  });
  window.addEventListener("offline", async () => {
    if (small.matches && (mic || voiceSocket || voiceStarting)) {
      await stopVoice();
      voiceHelp.textContent =
        "Connection lost. Reconnect Tailscale, then start the conversation again.";
    }
  });
  function sync(view) {
    document.body.dataset.phoneView = view === "Voice" ? "voice" : "workspace";
    nav
      .querySelectorAll("button")
      .forEach((n) =>
        n.setAttribute(
          "aria-current",
          n.dataset.phone === view ? "page" : "false",
        ),
      );
  }
  small.addEventListener("change", () => {
    arrangeVoiceSettings();
    sync(current);
  });
  sync(current);
  if (location.protocol === "https:") $("#phone-login").hidden = false;
  $("#phone-pair-form").onsubmit = async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button");
    submit.disabled = true;
    $("#phone-pair-error").textContent = "";
    try {
      await api("/auth/phone/redeem", {
        code: $("#phone-code").value,
        remember: $("#phone-remember").checked,
      });
      $("#phone-code").value = "";
      await init();
    } catch (error) {
      $("#phone-pair-error").textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  };
  let expiryTimer;
  $("#pair-phone").onclick = async () => {
    const body = el("section", undefined, "phone-pairing");
    showDetail("Take JARVIS with you", "");
    $("#detail-body").replaceChildren(body);
    try {
      const state = await api("/auth/phone");
      if (!state.configured) {
        body.append(
          el(
            "p",
            "Private phone access needs setup on your Nano. No accounts or credentials have changed.",
          ),
        );
        return;
      }
      body.append(
        el(
          "p",
          "Connect your iPhone to the Nano’s Tailscale network. Open this address in normal Safari or Chrome:",
        ),
      );
      const link = el("a", state.url);
      link.href = state.url;
      body.append(link);
      body.append(
        el(
          "p",
          "Create a one-time code below, then enter it on your phone. Creating another code replaces the previous one. Never share the code.",
        ),
      );
      const output = el("p", "", "pair-code");
      const expires = el("p", "", "help");
      const create = button(
        "Create pairing code",
        async () => {
          create.disabled = true;
          try {
            const grant = await api("/auth/phone/pair", {});
            if (!$("#detail").open || !body.isConnected) return;
            output.textContent = grant.code.match(/.{1,4}/g).join(" ");
            expires.textContent =
              "Code expires in two minutes. On your phone, select Keep this browser signed in for 30 days.";
            clearTimeout(expiryTimer);
            expiryTimer = setTimeout(() => {
              output.textContent = "";
              expires.textContent =
                "Code expired. Create a new code when your phone is ready.";
            }, grant.expires_in * 1000);
          } finally {
            create.disabled = false;
          }
        },
        "primary",
      );
      body.append(
        create,
        output,
        expires,
        el(
          "p",
          "For a demo: connect Tailscale → open JARVIS → tap Start Conversation and allow the microphone. Brain opens your real saved knowledge. Use Safari’s Share menu → Add to Home Screen for a shortcut. Keep the Nano powered on and online.",
        ),
      );
      if (state.devices?.length) {
        body.append(el("h3", "Paired phone browsers"));
        body.append(
          el(
            "p",
            "Revoke a lost or unused browser here. Lock signs out only the browser you are using.",
          ),
        );
        for (const device of state.devices) {
          const row = el("div", undefined, "card");
          row.dataset.deviceId = device.device_id;
          row.append(
            el(
              "p",
              `${device.remember ? "Remembered" : "Temporary"} phone browser · Paired ${new Date(device.created_at * 1000).toLocaleString()} · Expires ${new Date(device.expires * 1000).toLocaleDateString()}`,
            ),
          );
          row.append(
            button("Revoke access", async () => {
              await api("/auth/phone/revoke", { device_id: device.device_id });
              row.replaceChildren(
                el("p", "Access revoked. That browser must pair again."),
              );
            }),
          );
          body.append(row);
        }
      }
    } catch (error) {
      body.append(el("p", error.message));
    }
  };
  $("#detail").addEventListener("close", () => {
    clearTimeout(expiryTimer);
    $(".pair-code")?.replaceChildren();
  });
  return {
    sync,
    isPhone: () => small.matches,
    openVoice: () => navigate("Voice"),
  };
})();
