"""Deterministic, escaped, paginated PDF layout for human editorial review."""

from html import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer


def render(destination, edition, content, image_path):
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "Kicker",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=colors.HexColor("#087f98"),
            spaceAfter=10,
        )
    )
    styles.add(ParagraphStyle("Body", fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=9))
    styles.add(
        ParagraphStyle("Source", fontName="Helvetica", fontSize=8, leading=11, spaceAfter=7, wordWrap="CJK")
    )
    styles["Title"].fontSize = 23
    styles["Title"].leading = 28
    styles["Title"].alignment = 0
    story = []

    def p(text, style="Body"):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[style])

    def heading(kicker, title):
        story.extend([p(kicker, "Kicker"), p(title, "Title"), Spacer(1, 10)])

    heading("JARVIS / EDITORIAL REVIEW / " + edition, content["report"]["title"])
    story.append(p("Proposed LinkedIn post", "Heading2"))
    story.append(p(content["copy"]["text"]))
    story.append(p("Review before posting", "Heading3"))
    story.append(p(content["copy"]["review_notes"]))
    story.append(
        p(
            "Prepared for Demo User. Unpublished draft for personal review. Verify facts, product fit and HP communication requirements before posting.",
            "Source",
        )
    )
    story.append(PageBreak())
    heading("01 / RESEARCH BRIEF", "What changed. Why it matters.")
    for item in content["report"]["developments"]:
        story.append(
            KeepTogether([p(item["headline"], "Heading2"), p(item["fact"] + f" [{item['source']}]")])
        )
        for label, key in (
            ("Higher education and research", "significance"),
            ("HP / ZGX evaluation opportunity", "hp_opportunity"),
            ("Suggested next action", "suggested_action"),
            ("Caveat", "caveat"),
        ):
            story.append(p(label + ": " + item[key]))
        story.append(Spacer(1, 8))
    story.append(PageBreak())
    heading("02 / VISUAL DIRECTION", "Make the idea visible.")
    width, height = ImageReader(str(image_path)).getSize()
    display_width = 390
    story.append(Image(str(image_path), width=display_width, height=display_width * height / width))
    story.append(Spacer(1, 12))
    story.append(
        p(
            "AI-generated conceptual illustration. Not a photograph of an actual HP product or research deployment.",
            "Source",
        )
    )
    story.append(p("Alt text: " + content["visual"]["alt_text"]))
    story.append(p(content["visual"]["rationale"]))
    story.append(PageBreak())
    heading("03 / EVIDENCE & PRODUCTION NOTES", "Check the source, then publish.")
    used = sorted({item["source"] for item in content["report"]["developments"]})
    for number in used:
        source = content["sources"][number - 1]
        story.append(p(f"[{number}] {source['title']}", "Heading3"))
        story.append(
            p(
                f"{source['publisher']} | Published: {source.get('at', 'Unavailable')} | Retrieved: {source.get('retrieved_at', 'Unavailable')}",
                "Source",
            )
        )
        story.append(p(source["url"], "Source"))
        story.append(p("Coverage: " + source.get("coverage", "feed_excerpt"), "Source"))
    story.append(p("Visual generation prompt", "Heading3"))
    story.append(p(content["visual"]["prompt"], "Source"))
    story.append(
        p(
            "Model: "
            + content["image"].get("model", "Local model")
            + ". Research and writing use the existing local JARVIS primary model. Source coverage is selective; vendor statements are not independent verification.",
            "Source",
        )
    )
    failed = [s["publisher"] for s in content.get("providers", []) if s["status"] == "unavailable"]
    if failed:
        story.append(p("Unavailable publishers: " + ", ".join(failed), "Source"))

    def page(canvas, doc):
        canvas.setStrokeColor(colors.HexColor("#d4e3e8"))
        canvas.line(48, 43, 564, 43)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#52606d"))
        canvas.drawString(48, 30, "JARVIS  |  Demo User  |  Review before publication")
        canvas.drawRightString(564, 30, str(doc.page))

    temporary = destination.with_suffix(".tmp")
    SimpleDocTemplate(
        str(temporary),
        pagesize=(612, 792),
        rightMargin=48,
        leftMargin=48,
        topMargin=44,
        bottomMargin=58,
        title="JARVIS LinkedIn review | " + edition,
        author="JARVIS for Demo User",
    ).build(story, onFirstPage=page, onLaterPages=page)
    temporary.replace(destination)
