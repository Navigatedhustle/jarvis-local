"use strict";
globalThis.JarvisPackets = (() => {
  async function panel() {
    const box = el("section", undefined, "record");
    box.setAttribute("aria-label", "Weekday LinkedIn studio");
    async function load() {
      const data = await api("/content-packets");
      box.replaceChildren(
        el("small", "RESEARCH → COPY → VISUAL → YOUR REVIEW", "packet-kicker"),
        el("h2", "Weekday LinkedIn studio"),
        el(
          "p",
          "A sourced AI briefing, a useful post for higher education and research, and an original visual. Prepared at 5 AM Denver time, Monday through Friday.",
        ),
        el(
          "p",
          `${data.enabled ? "Scheduled" : "Schedule paused"} · Next run ${time(data.next_run)}`,
        ),
        el(
          "small",
          data.delivery_ready
            ? `Weekday delivery to ${data.recipient} is enabled. Only these review packets may be sent automatically. Nothing is posted to LinkedIn.`
            : data.delivery_grant?.validated
              ? "Self-email delivery is paused. Prepared packets remain available here."
              : `Delivery to ${data.recipient} awaits Outlook attachment-send validation${data.delivery_grant?.authorized ? "" : " and your approval"}. Nothing is posted to LinkedIn.`,
        ),
      );
      const toolbar = el("div", undefined, "workspace-toolbar");
      toolbar.append(
        button("Prepare today's packet", async () => {
          try {
            await api("/content-packets/prepare", {});
            await load();
            toast(
              "Packet queued. Research and generation run in the background.",
            );
          } catch (error) {
            toast(error.message);
          }
        }),
        button(
          data.enabled ? "Pause weekday schedule" : "Enable weekday schedule",
          async () => {
            try {
              await api("/content-packets/preferences", {
                enabled: !data.enabled,
              });
              await load();
            } catch (error) {
              toast(error.message);
            }
          },
        ),
        button("Refresh packet status", load),
      );
      box.append(toolbar);
      if (data.delivery_grant?.validated)
        toolbar.append(
          button(
            data.delivery_ready
              ? "Pause self-email delivery"
              : "Resume self-email delivery",
            async () => {
              await api("/content-packets/delivery-preferences", {
                enabled: !data.delivery_ready,
              });
              await load();
            },
          ),
        );
      for (const packet of data.packets) {
        const delivery = (data.deliveries || []).find(
          (item) => item.edition === packet.edition,
        );
        const article = el("article", undefined, "record");
        article.append(
          el(
            "h3",
            `${packet.edition} · ${packet.content.report?.title || "AI editorial packet"}`,
          ),
        );
        const failed = packet.job_status === "failed";
        const label =
          packet.status === "ready"
            ? delivery?.status === "succeeded"
              ? "Ready for review · Sent to your HP inbox"
              : ["uncertain", "reserved"].includes(delivery?.status)
                ? "Ready for review · Delivery needs reconciliation; automatic retry blocked"
                : "Ready for review · Email not sent"
            : packet.status === "no_news"
              ? "No verified recent news · No post fabricated"
              : failed
                ? `Needs attention at ${packet.stage}; use Prepare today to retry`
                : `${packet.job_status || packet.status} · ${packet.stage}`;
        article.append(el("p", label));
        if (packet.content.copy) {
          const details = el("details");
          details.append(el("summary", "Preview LinkedIn copy"));
          for (const paragraph of packet.content.copy.text.split(/\n\s*\n/))
            details.append(el("p", paragraph));
          article.append(details);
        }
        if (packet.status === "ready") {
          const visual = el("details");
          visual.append(el("summary", "Preview visual"));
          visual.ontoggle = () => {
            if (visual.open && !visual.querySelector("img")) {
              const image = el("img", undefined, "packet-image");
              image.alt =
                packet.content.visual?.alt_text ||
                "AI-generated conceptual illustration for the proposed post";
              image.src = `/api/content-packets/${encodeURIComponent(packet.edition)}/download/image`;
              visual.append(image);
            }
          };
          article.append(visual);
          const downloads = el(
            "div",
            undefined,
            "workspace-toolbar packet-downloads",
          );
          if (delivery)
            downloads.append(
              button("View delivery record", () => navigate("Approvals")),
            );
          else if (!data.delivery_grant?.validated)
            downloads.append(
              button("Prepare self-email approval", async () => {
                try {
                  await api(
                    `/content-packets/${encodeURIComponent(packet.edition)}/propose-delivery`,
                    {},
                  );
                  navigate("Approvals");
                } catch (error) {
                  toast(error.message);
                }
              }),
            );
          for (const [asset, label] of [
            ["pdf", "Download review PDF"],
            ["image", "Download image"],
            ["copy", "Download LinkedIn copy"],
          ]) {
            const link = el("a", label, "packet-download");
            link.href = `/api/content-packets/${encodeURIComponent(packet.edition)}/download/${asset}`;
            link.setAttribute("download", "");
            downloads.append(link);
          }
          article.append(downloads);
        }
        const advanced = el("details");
        advanced.append(
          el("summary", "Run details"),
          el(
            "p",
            `Stage: ${packet.stage}. Queue: ${packet.job_status || "not queued"}. Last update: ${time(packet.updated_at)}. ${packet.error ? "Last error: " + packet.error + "." : ""}`,
          ),
        );
        if (packet.content.image)
          advanced.append(
            el(
              "p",
              `Local visual: ${packet.content.image.model}. Generation: ${packet.content.image.generation_seconds}s. The image model unloads after use.`,
            ),
          );
        article.append(advanced);
        box.append(article);
      }
      if (!data.packets.length)
        box.append(
          el(
            "p",
            "Your first packet will appear here when preparation starts.",
          ),
        );
    }
    try {
      await load();
    } catch {
      box.append(el("p", "Packet status is temporarily unavailable."));
    }
    return box;
  }
  return { panel };
})();
