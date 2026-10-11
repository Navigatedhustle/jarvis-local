"""Turn segmentation, local speech pipeline and cancellable sentence playback."""

import asyncio
import base64
import io
import math
import re
import time
import wave
from array import array
from contextlib import nullcontext

import httpx


class SpeechDetector:
    """WebRTC acoustic speech classifier, accepting arbitrary PCM frame boundaries."""

    def __init__(self):
        import webrtcvad

        self.vad = webrtcvad.Vad(2)
        self.buffer = bytearray()
        self.voiced = False

    def __call__(self, pcm):
        self.buffer.extend(pcm)
        while len(self.buffer) >= 640:
            frame = bytes(self.buffer[:640])
            del self.buffer[:640]
            self.voiced = self.vad.is_speech(frame, 16000)
        return self.voiced


class Segmenter:
    """16 kHz PCM16; 80 ms onset, 850 ms silence end, 20 s cap and 120 ms pre-roll."""

    def __init__(self, rate=16000, threshold=0.006, detector=None):
        self.rate, self.threshold = rate, threshold
        self.frames = bytearray()
        self.silence = 0
        self.speech = False
        self.voiced_samples = 0
        self.onset_samples = 0
        self.preroll = bytearray()
        self.detector = detector
        self.rms = 0.0
        self.noise_floor = 0.001
        self.completed_silence_samples = 0
        self.completed = False

    def feed(self, pcm):
        self.completed = False
        if len(pcm) % 2 or len(pcm) > 64000:
            raise ValueError("Invalid audio frame")
        samples = array("h", pcm)
        rms = math.sqrt(sum((x / 32768) ** 2 for x in samples) / max(1, len(samples)))
        self.rms = rms
        voiced = rms >= max(self.threshold, self.noise_floor * 2.5)
        if self.detector is not None:
            voiced = voiced and self.detector(pcm)
        if not self.speech and not voiced:
            self.noise_floor = min(0.012, self.noise_floor * 0.98 + rms * 0.02)
        began = False
        if not self.speech:
            self.preroll.extend(pcm)
            self.preroll = self.preroll[-int(self.rate * 2 * 0.12) :]
            self.onset_samples = self.onset_samples + len(samples) if voiced else 0
            if self.onset_samples < self.rate * 0.08:
                return False, None
            began = True
            self.speech = True
            self.frames.extend(self.preroll[: -len(pcm)] if len(pcm) < len(self.preroll) else b"")
            self.voiced_samples = self.onset_samples - len(samples)
            self.preroll.clear()
        if voiced:
            self.speech = True
            self.silence = 0
            self.voiced_samples += len(samples)
        if self.speech:
            self.frames.extend(pcm)
            if not voiced:
                self.silence += len(samples)
        complete = self.speech and (
            self.silence >= self.rate * 0.85 or len(self.frames) >= self.rate * 2 * 20
        )
        if complete:
            self.completed = True
            self.completed_silence_samples = self.silence
            data = bytes(self.frames) if self.voiced_samples >= self.rate * 0.15 else None
            self.frames.clear()
            self.speech = False
            self.silence = 0
            self.voiced_samples = 0
            self.onset_samples = 0
            return began, data
        return began, None


def wav_bytes(pcm, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def chunks(text, limit=350, first_limit=220):
    first = True
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        # Preserve a natural sentence even when it exceeds the soft first-audio
        # target. Split at a clause boundary before falling back to a word boundary.
        while len(sentence) > limit:
            bound = first_limit if first else limit
            clauses = list(re.finditer(r"[,;:]\s+", sentence[:bound]))
            index = clauses[-1].start() + 1 if clauses else sentence.rfind(" ", 0, bound)
            index = index if index > 0 else bound
            yield sentence[:index]
            first = False
            sentence = sentence[index:].strip()
        if sentence:
            yield sentence
            first = False


def spoken_text(text):
    """Keep evidence on screen without reading citation numbers, URLs or opaque IDs aloud."""
    text = re.sub(r"\[(\d+)\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "shown on screen",
        text,
        flags=re.I,
    )
    text = re.sub(r"(?m)^\s*(?:#{1,6}|[-*]|\d+\.)\s+", "", text)
    text = re.sub(r"[ \t]+", " ", text.replace("**", "").replace("`", "")).strip()
    if re.fullmatch(r"\d+[.)]", text) or not any(c.isalnum() for c in text):
        return ""  # Streaming list markers/citation punctuation are not speech turns.
    return text


class ReasoningDeadline:
    """Charge generation for active time, with a finite allowance for GPU speech leases."""

    def __init__(self, timeout, speech_allowance=30):
        self.started = time.monotonic()
        self.timeout, self.speech_allowance = timeout, speech_allowance
        self.paused = 0.0
        self.pause_started = None

    def remaining(self):
        now = time.monotonic()
        paused = self.paused + (now - self.pause_started if self.pause_started is not None else 0)
        elapsed = now - self.started
        return min(
            self.timeout + self.speech_allowance - elapsed,
            self.timeout - elapsed + min(paused, self.speech_allowance),
        )


class Voice:
    reasoning_timeout = 30

    def __init__(self, jarvis, db):
        self.jarvis, self.db = jarvis, db

    async def speak(self, text, send, turn_id, client, metrics, deadline=None):
        await send({"type": "state", "state": "synthesizing", "turn_id": turn_id})
        for chunk in chunks(spoken_text(text)):
            metrics.setdefault("tts_requested", time.monotonic())
            started = time.monotonic()
            priority = getattr(self.jarvis.inference, "speech_priority", None)
            async with priority() if priority else nullcontext() as leased:
                if leased and deadline is not None:
                    deadline.pause_started = time.monotonic()
                try:
                    response = await client.post(
                        "http://127.0.0.1:8186/synthesize", json={"text": chunk}, timeout=30
                    )
                finally:
                    if leased and deadline is not None:
                        deadline.paused += time.monotonic() - deadline.pause_started
                        deadline.pause_started = None
            response.raise_for_status()
            if self.db:
                self.db.audit(
                    "voice.synthesis",
                    turn_id,
                    {
                        "characters": len(chunk),
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "cache": response.headers.get("X-Audio-Cache", "miss"),
                        "gpu_handoff": bool(leased),
                    },
                )
            metrics.setdefault("first_audio", time.monotonic())
            await send(
                {
                    "type": "audio",
                    "data": base64.b64encode(response.content).decode(),
                    "turn_id": turn_id,
                }
            )

    async def preview(self, send, turn_id):
        metrics = {"speech_end": time.monotonic()}
        self.jarvis.inference.interactive += 1
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                await self.speak(
                    "At your service, sir. Your time is rather valuable. Let's spend it wisely.",
                    send,
                    turn_id,
                    client,
                    metrics,
                )
                await send({"type": "done", "turn_id": turn_id})
        finally:
            self.jarvis.inference.interactive -= 1

    async def turn(self, pcm, send, actor, turn_id, speech_end=None, recognized=None):
        metrics = {
            "turn_id": turn_id,
            "speech_end": speech_end if speech_end is not None else time.monotonic(),
        }
        outcome = "completed"
        text = ""
        speech_task = None
        self.jarvis.inference.interactive += 1
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                await send({"type": "state", "state": "transcribing", "turn_id": turn_id})
                if recognized is None:
                    metrics["asr_started"] = time.monotonic()
                    result = await client.post(
                        "http://127.0.0.1:8185/transcribe",
                        files={"file": ("turn.wav", wav_bytes(pcm), "audio/wav")},
                        timeout=8,
                    )
                    result.raise_for_status()
                    text = result.json()["text"].strip()
                    metrics["stt_complete"] = time.monotonic()
                else:
                    text, metrics["asr_started"], metrics["stt_complete"] = recognized
                if not text:
                    await send({"type": "empty", "turn_id": turn_id})
                    return
                await send({"type": "transcript", "text": text, "turn_id": turn_id})
                self.db.audit(
                    "voice.transcript",
                    turn_id,
                    {"text": text},
                    actor="demo",
                    source=getattr(actor, "channel", "dashboard"),
                )
                await send({"type": "state", "state": "thinking", "turn_id": turn_id})
                queue = asyncio.Queue()  # Bounded by the model's existing output token limit.
                pending_text = ""
                streamed = []
                spoken_words = 0

                async def enqueue(part):
                    nonlocal spoken_words
                    clean = spoken_text(part)
                    words = len(clean.split())
                    if not clean or spoken_words >= 90:
                        return
                    if not spoken_words and words > 100:
                        clean = "I have the detailed answer on screen. Ask me to focus on one part."
                        words = len(clean.split())
                        spoken_words = 90  # Never cut a long factual sentence mid-claim.
                    # Keep complete sentences, and leave extended detail on screen.
                    if spoken_words and spoken_words + words > 100:
                        return
                    spoken_words += words
                    await queue.put(clean)

                async def phrase(delta):
                    nonlocal pending_text
                    pending_text += delta
                    parts = re.split(r"(?<=[.!?])\s+", pending_text)
                    for part in parts[:-1]:
                        if part.strip():
                            metrics.setdefault("llm_first_phrase", time.monotonic())
                            streamed.append(part)
                            await enqueue(part)
                    pending_text = parts[-1]

                deadline = ReasoningDeadline(self.reasoning_timeout)

                async def speak_queue():
                    while True:
                        part = await queue.get()
                        if part is None:
                            return
                        await self.speak(part, send, turn_id, client, metrics, deadline)

                speech_task = asyncio.create_task(speak_queue())

                async def activity(status):
                    metrics.setdefault(
                        "retrieval_started" if status["active"] else "retrieval_complete", time.monotonic()
                    )
                    await send({"type": "activity", "turn_id": turn_id, **status})

                pending_reply = asyncio.create_task(
                    self.jarvis.reply(
                        text,
                        actor=actor,
                        spoken=True,
                        on_first_token=lambda: metrics.setdefault("llm_first_token", time.monotonic()),
                        on_text=phrase,
                        on_activity=activity,
                    )
                )
                try:
                    # Synthesis deliberately pauses generation on the shared GPU.
                    # Discount only confirmed lease time, never an unbounded audio grace.
                    progress_sent = False
                    done = set()
                    while deadline.remaining() > 0:
                        done, _ = await asyncio.wait(
                            {pending_reply, speech_task},
                            timeout=min(0.25, deadline.remaining()),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if speech_task in done:
                            speech_task.result()
                        if pending_reply in done:
                            break
                        if not progress_sent and time.monotonic() - deadline.started >= 4:
                            progress_sent = True
                            await send(
                                {
                                    "type": "progress",
                                    "turn_id": turn_id,
                                    "text": "Still checking the details. You can interrupt me.",
                                }
                            )
                    if pending_reply not in done:
                        raise TimeoutError("Voice reasoning deadline exceeded")
                    answer = pending_reply.result()
                except TimeoutError:
                    self.db.audit("voice.reasoning_timeout", turn_id)
                    outcome = "degraded"
                    speech_task.cancel()
                    await asyncio.gather(speech_task, return_exceptions=True)
                    queue = asyncio.Queue()
                    speech_task = asyncio.create_task(speak_queue())
                    streamed.clear()
                    pending_text = ""
                    spoken_words = 0
                    answer = {
                        "reply": "That took too long to produce a reliable answer. Your request hasn't completed. You can ask a narrower question or use the dashboard while I recover."
                    }
                finally:
                    if not pending_reply.done():
                        pending_reply.cancel()
                    await asyncio.gather(pending_reply, return_exceptions=True)
                metrics["llm_complete"] = time.monotonic()
                await send(
                    {
                        "type": "reply",
                        "text": answer["reply"],
                        "sources": answer.get("sources", []),
                        "message_id": answer.get("message_id"),
                        "turn_id": turn_id,
                    }
                )
                if streamed:
                    if pending_text.strip():
                        await enqueue(pending_text)
                else:
                    for part in re.split(r"(?<=[.!?])\s+", answer["reply"]):
                        await enqueue(part)
                await queue.put(None)
                await speech_task
                timings = {
                    key: round((value - metrics["speech_end"]) * 1000)
                    for key, value in metrics.items()
                    if key != "turn_id"
                }
                await send({"type": "done", "turn_id": turn_id, "timings": timings})
                self.db.audit(
                    "voice.completed",
                    turn_id,
                    timings,
                )
        except asyncio.CancelledError:
            outcome = "interrupted"
            if text:
                self.db.set_state("conversation.interrupted.demo", {"request": text, "turn_id": turn_id})
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            if speech_task and not speech_task.done():
                speech_task.cancel()
                await asyncio.gather(speech_task, return_exceptions=True)
            self.jarvis.inference.interactive -= 1
            self.db.audit(
                "voice.turn_ended",
                turn_id,
                {
                    "outcome": outcome,
                    **{
                        key: round((value - metrics["speech_end"]) * 1000)
                        for key, value in metrics.items()
                        if key != "turn_id"
                    },
                },
                source=getattr(actor, "channel", "internal"),
            )
