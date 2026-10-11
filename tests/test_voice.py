import asyncio
import io
import wave
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from jarvis.voice import Segmenter, chunks, wav_bytes


def test_silence_does_not_create_turn_and_speech_segments():
    vad = Segmenter()
    silence = array("h", [0] * 320).tobytes()
    speech = array("h", [10000] * 320).tobytes()
    for _ in range(60):
        assert vad.feed(silence) == (False, None)
    for _ in range(3):
        assert not vad.feed(speech)[0]
    assert vad.feed(speech)[0]
    for _ in range(15):
        vad.feed(speech)
    completed = []
    for _ in range(50):
        began, turn = vad.feed(silence)
        if turn:
            completed.append(turn)
    assert len(completed) == 1
    with wave.open(io.BytesIO(wav_bytes(completed[0]))) as wav:
        assert wav.getframerate() == 16000 and wav.getnchannels() == 1


def test_speech_chunks_bounded():
    text = "First sentence. " + ("A detailed point. " * 60)
    parts = list(chunks(text))
    assert parts[0] == "First sentence."
    assert max(map(len, parts)) <= 350
    parts = list(chunks("A useful detailed phrase " * 35))
    assert len(parts[0]) <= 220
    assert max(map(len, parts)) <= 350


def test_speech_preserves_sentence_prosody():
    sentence = "Your meeting with the research team starts on Monday at nine, and I suggest reviewing the agenda and the recent discussion before you join."
    assert len(sentence) > 100
    assert list(chunks(sentence)) == [sentence]


def test_short_noise_does_not_interrupt_but_sustained_speech_does():
    vad = Segmenter()
    click = array("h", [10000] * 320).tobytes()
    silence = bytes(640)
    assert vad.feed(click) == (False, None)
    for _ in range(40):
        assert vad.feed(silence) == (False, None)
    assert sum(vad.feed(click)[0] for _ in range(4)) == 1


def test_spoken_rendering_keeps_evidence_and_ids_on_screen():
    from jarvis.voice import spoken_text

    result = spoken_text(
        "**Review the meeting.** [2]\n- Open [the source](https://example.invalid).\nAction 12345678-1234-1234-1234-123456789abc."
    )
    assert "Review the meeting." in result
    assert "[2]" not in result and "https" not in result and "12345678" not in result
    assert "the source" in result and "shown on screen" in result


@pytest.mark.parametrize("fragment", ["1.", "2.", "3)", "[1].", ".", " "])
def test_streaming_list_markers_are_not_synthesized(fragment):
    from jarvis.voice import spoken_text

    assert spoken_text(fragment) == ""


@pytest.mark.parametrize("transcript", ["", "Prepare me for my next meeting."])
async def test_voice_states_sources_and_priority_cleanup(monkeypatch, transcript):
    from jarvis import voice

    spoken = []

    def handler(request):
        if request.url.path == "/transcribe":
            return httpx.Response(200, json={"text": transcript})
        import json

        spoken.append(json.loads(request.content)["text"])
        return httpx.Response(200, content=wav_bytes(bytes(3200)))

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handler))
    )
    jarvis = SimpleNamespace(
        inference=SimpleNamespace(interactive=0),
        reply=AsyncMock(return_value={"reply": "Review the agenda. [1]", "sources": [{"title": "Meeting"}]}),
    )
    db = SimpleNamespace(audit=Mock())
    sent = []

    async def send(event):
        sent.append(event)

    await voice.Voice(jarvis, db).turn(bytes(3200), send, None, "turn")
    assert jarvis.inference.interactive == 0
    if not transcript:
        assert sent[-1]["type"] == "empty"
        jarvis.reply.assert_not_awaited()
    else:
        assert [e["state"] for e in sent if e["type"] == "state"] == [
            "transcribing",
            "thinking",
            "synthesizing",
        ]
        assert next(e for e in sent if e["type"] == "reply")["sources"]
        assert sent[-1]["type"] == "done" and "first_audio" in sent[-1]["timings"]
        assert spoken == ["Review the agenda."]
        assert jarvis.reply.await_args.kwargs["spoken"] is True


async def test_voice_failure_releases_interactive_priority(monkeypatch):
    from jarvis import voice

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx,
        "AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(lambda request: httpx.Response(503))),
    )
    jarvis = SimpleNamespace(inference=SimpleNamespace(interactive=0))
    with pytest.raises(httpx.HTTPStatusError):
        await voice.Voice(jarvis, None).preview(AsyncMock(), "sample")
    assert jarvis.inference.interactive == 0


async def test_first_sentence_audio_precedes_finished_llm_answer(monkeypatch):
    from jarvis import voice

    heard = asyncio.Event()

    async def reply(*args, **kwargs):
        await kwargs["on_text"]("First sentence. ")
        await asyncio.wait_for(heard.wait(), 1)
        await kwargs["on_text"]("Second sentence.")
        return {"reply": "First sentence. Second sentence."}

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, json={"text": "Question"})
                    if r.url.path == "/transcribe"
                    else httpx.Response(200, content=wav_bytes(bytes(3200)))
                )
            )
        ),
    )
    events = []

    async def send(event):
        events.append(event)
        if event["type"] == "audio":
            heard.set()

    jarvis = SimpleNamespace(inference=SimpleNamespace(interactive=0), reply=reply)
    await voice.Voice(jarvis, SimpleNamespace(audit=Mock())).turn(bytes(3200), send, None, "streamed")
    kinds = [event["type"] for event in events]
    assert kinds.index("audio") < kinds.index("reply")
    assert kinds.count("audio") == 2 and jarvis.inference.interactive == 0


def test_acoustic_detector_rejects_digital_silence():
    from jarvis.voice import SpeechDetector

    detector = SpeechDetector()
    assert not any(detector(bytes(256)) for _ in range(100))


def test_reasoning_deadline_discounts_only_leased_speech_and_remains_bounded(monkeypatch):
    from jarvis import voice

    clock = [0.0]
    monkeypatch.setattr(voice.time, "monotonic", lambda: clock[0])
    deadline = voice.ReasoningDeadline(30)
    clock[0] = 20
    assert deadline.remaining() == 10
    deadline.pause_started = 20
    clock[0] = 45
    assert deadline.remaining() == 10  # Primary has been paused, not stalled.
    deadline.paused = 25
    deadline.pause_started = None
    clock[0] = 50
    assert deadline.remaining() == 5
    clock[0] = 55
    assert deadline.remaining() == 0
    # Even continuously renewed leases cannot hold a turn indefinitely.
    deadline.pause_started = 55
    clock[0] = 61
    assert deadline.remaining() < 0


async def test_voice_deadline_cancels_reasoning_and_delivers_failure_explanation(monkeypatch):
    from jarvis import voice

    cancelled = asyncio.Event()

    async def stalled(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, json={"text": "Question"})
                    if r.url.path == "/transcribe"
                    else httpx.Response(200, content=wav_bytes(bytes(3200)))
                )
            )
        ),
    )
    jarvis = SimpleNamespace(inference=SimpleNamespace(interactive=0), reply=stalled)
    worker = voice.Voice(jarvis, SimpleNamespace(audit=Mock()))
    worker.reasoning_timeout = 0.01
    sent = []

    async def send(event):
        sent.append(event)

    await worker.turn(bytes(3200), send, None, "deadline")
    assert cancelled.is_set() and jarvis.inference.interactive == 0
    assert "hasn't completed" in next(e["text"] for e in sent if e["type"] == "reply")
    assert any(e["type"] == "audio" for e in sent)
    assert sent[-1]["type"] == "done"


@pytest.mark.parametrize("leased", [True, False])
async def test_streaming_reply_survives_real_speech_pause_only(monkeypatch, leased):
    from contextlib import asynccontextmanager

    from jarvis import voice

    speech_finished = asyncio.Event()

    @asynccontextmanager
    async def priority():
        yield leased

    async def handler(request):
        if request.url.path == "/transcribe":
            return httpx.Response(200, json={"text": "Summarize my saved mail"})
        await asyncio.sleep(0.3)
        speech_finished.set()
        return httpx.Response(200, content=wav_bytes(bytes(3200)))

    async def reply(*args, **kwargs):
        await kwargs["on_text"]("Five messages found. ")
        await speech_finished.wait()
        return {"reply": "Five messages found.", "sources": [{"id": "saved"}]}

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handler))
    )
    jarvis = SimpleNamespace(inference=SimpleNamespace(interactive=0, speech_priority=priority), reply=reply)
    worker = voice.Voice(jarvis, SimpleNamespace(audit=Mock()))
    worker.reasoning_timeout = 0.15
    sent = []

    async def send(event):
        sent.append(event)

    await worker.turn(bytes(3200), send, None, "lease-test")
    result = next(e for e in sent if e["type"] == "reply")
    assert bool(result["sources"]) is leased
    assert ("hasn't completed" in result["text"]) is not leased
    assert jarvis.inference.interactive == 0


def test_normal_pause_does_not_end_a_turn():
    segmenter = Segmenter()
    for _ in range(20):
        segmenter.feed(array("h", [5000] * 320).tobytes())
    assert not any(segmenter.feed(bytes(640))[1] for _ in range(35))
    assert segmenter.speech


def test_voice_profiles_are_allowlisted_and_differ_in_real_tts_parameters():
    from jarvis.voice_profiles import profile

    with pytest.raises(ValueError):
        profile("../../secrets/runtime.env")
    composed, restrained = profile("british-balanced"), profile("british-restrained")
    assert composed["reference"] == restrained["reference"]
    assert composed["cfg_weight"] != restrained["cfg_weight"]
    assert composed["seed"] == restrained["seed"]


def test_spoken_calendar_followup_preserves_day_and_real_source(db):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from jarvis.intelligence import ingest_event
    from jarvis.meetings import spoken_calendar

    tomorrow = datetime.now(ZoneInfo("America/Denver")).replace(
        hour=9, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    for name, hour in [("first", 0), ("second", 2)]:
        ingest_event(
            db,
            {
                "id": name,
                "subject": "Synthetic " + name + " meeting",
                "start": {
                    "dateTime": (tomorrow + timedelta(hours=hour)).astimezone(timezone.utc).isoformat()
                },
            },
        )
    db.set_state("outlook.last_sync", datetime.now(timezone.utc).isoformat())
    brief = spoken_calendar(db, "qa")
    assert "2 meetings saved for tomorrow" in brief["reply"]
    first = spoken_calendar(db, "qa", first_only=True)
    assert first["sources"][0]["id"] == "graph:event:first"
    assert "second meeting" not in first["reply"]
    assert "9:00 AM" in first["reply"]
