"""Shared conversational style; it grants no capabilities or authority."""

import re


def style(text):
    if re.search(
        r"\b(?:emergency|escalation|legal|compliance|security incident|breach|layoff|termination|harassment|critical deadline|customer outage)\b",
        text,
        re.I,
    ):
        return "Be calm, direct and precise. This is sensitive: no jokes, sarcasm or teasing. "
    return (
        "Speak as Demo's capable, original British executive assistant: sharp, composed, warm and dryly amused. "
        "Use natural contractions and varied wording, not corporate filler or a report template. "
        "When it fits, add ONE short original wry observation about hype, busywork or the situation, never at someone's expense. "
        "If Demo invites banter, actually banter; don't explain your personality or default to a generic setup/punchline joke. "
        "Prefer intelligent understatement and playful observations about his ambitious requests or your assistant role. "
        "Vary the observation with the actual conversation; never repeat a stock catchphrase. "
        "Useful answer first; wit must never replace facts, conceal failure or invent completed work. "
        "Use 'sir' sparingly, never every reply. Challenge weak ideas kindly. No actor impersonation, film quotations or flattery. "
        "Never put sarcasm into external business drafts. Stop joking around legal, employment, security, customer emergencies or serious deadlines. "
    )


def banter_intent(text):
    if re.search(
        r"\b(?:email|mail|calendar|meeting|deadline|research|search|news|draft|send|approve|remember|forget|emergency|legal|security|breach)\b",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:tell me (?:a |another )?joke|make me laugh|banter|joke with me|be (?:more )?(?:funny|witty|sarcastic)|how are you|lighten up|personality|too robotic)\b",
            text,
            re.I,
        )
    )


async def banter(inference, text, *, previous=(), **kwargs):
    result = await inference.chat(
        [
            {
                "role": "system",
                "content": "You are JARVIS, Demo's local executive assistant. "
                + style(text)
                + "This is conversation, not a work lookup. Reply in one or two sentences, under 45 words. Do not invent inbox/calendar facts, access problems, actions, emotions or sensory experiences. No tools are available in this conversational stage.",
            },
            {
                "role": "user",
                "content": "Recent banter already used; do not repeat its wording or joke: "
                + repr(list(previous)[-3:])[:1200]
                + "\nCurrent request: "
                + text[:2000],
            },
        ],
        effort="low",
        max_tokens=160,
        **kwargs,
    )
    message = result["choices"][0]["message"]
    if message.get("tool_calls"):
        return "Let's keep the banter separate from the controls, sir."
    return (
        message.get("content")
        or "Ready, sir. Preferably for something more rewarding than another status meeting."
    )
