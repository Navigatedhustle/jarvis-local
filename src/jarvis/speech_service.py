"""Isolated local GPU speech worker. Published only to a loopback port."""

import asyncio
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import soundfile as sf
import torch
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from scipy.signal import resample_poly
from speech_cache import SpeechCache
from voice_profiles import PROFILES, profile

role = os.environ.get("JARVIS_SPEECH_ROLE", "stt")
model = None
processor = None
lock = asyncio.Lock()
speech_stats = {"pending": 0, "completed": 0, "abandoned": 0}
speech_cache = SpeechCache()
conditioning = {}
asr_model_id = os.environ.get("JARVIS_ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
selection_path = Path("/models/voices/selected.json")


@asynccontextmanager
async def lifespan(app):
    global model, processor
    if role == "stt":
        if "unified" in asr_model_id:
            from nemo.collections.asr.models import ASRModel

            model = ASRModel.from_pretrained(asr_model_id).to("cuda").eval()
            model.cfg.decoding.strategy = "greedy_batch"
            model.cfg.decoding.greedy.use_cuda_graph_decoder = False
            model.change_decoding_strategy(model.cfg.decoding)
        else:
            from transformers import AutoModelForTDT, AutoProcessor

            processor = AutoProcessor.from_pretrained(asr_model_id)
            model = AutoModelForTDT.from_pretrained(asr_model_id, dtype=torch.float32).to("cuda").eval()
    else:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
        reference = profile("british-balanced")["reference"]
        if reference:
            model.prepare_conditionals(reference, exaggeration=0.25)
            conditioning[reference] = model.conds
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    return {
        "status": "ready" if model is not None else "loading",
        "role": role,
        "model": asr_model_id if role == "stt" else "Chatterbox Multilingual V3",
        "mode": "buffered partials and final recognition"
        if "unified" in asr_model_id and role == "stt"
        else "local",
        **(
            {
                "speech_requests": dict(speech_stats),
                "audio_cache": {
                    "entries": len(speech_cache.items),
                    "bytes": speech_cache.bytes,
                    "hits": speech_cache.hits,
                },
            }
            if role == "tts"
            else {}
        ),
    }


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), partial: bool = False):
    if role != "stt":
        raise HTTPException(404)
    data = await file.read(12_000_001)
    if len(data) > 12_000_000:
        raise HTTPException(413)
    started = time.monotonic()
    try:
        audio, rate = sf.read(io.BytesIO(data), dtype="float32")
    except Exception:
        raise HTTPException(422, "A valid WAV, FLAC or OGG audio file is required") from None
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if len(audio) / rate > 120 or len(audio) / rate < 0.1:
        raise HTTPException(422, "Audio must last between 0.1 and 120 seconds")
    if rate != 16000:
        from math import gcd

        divisor = gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // divisor, rate // divisor)

    def infer():
        with torch.inference_mode():
            if "unified" in asr_model_id:
                # Unified chunk-attention mode for bounded previews, full attention for final correction.
                model.encoder.set_default_att_context_size([70, 7, 7] if partial else [-1, -1, -1])
                output = model.transcribe([audio], batch_size=1, verbose=False)
                return output[0].text
            inputs = processor(audio, sampling_rate=16000, return_tensors="pt").to(model.device)
            output = model.generate(**inputs, return_dict_in_generate=True)
            text = processor.batch_decode(output.sequences, skip_special_tokens=True)[0]
            return text

    async with lock:
        text = await asyncio.to_thread(infer)
    return {"text": text, "stt_ms": round((time.monotonic() - started) * 1000)}


class Speech(BaseModel):
    text: str = Field(min_length=1, max_length=700)
    candidate: str | None = None


@app.post("/voice/select")
async def select_voice(body: Speech):
    if role != "tts" or body.candidate not in PROFILES:
        raise HTTPException(422, "Unknown voice candidate")
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    async with lock:
        temp = selection_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"candidate": body.candidate}))
        temp.chmod(0o600)
        temp.replace(selection_path)
    return {"selected": body.candidate}


@app.post("/synthesize")
async def synthesize(body: Speech, request: Request):
    if role != "tts":
        raise HTTPException(404)
    started = time.monotonic()
    selected = (
        json.loads(selection_path.read_text()).get("candidate")
        if selection_path.exists()
        else "british-balanced"
    )
    try:
        config = profile(body.candidate or selected)
    except ValueError:
        raise HTTPException(422, "Unknown voice candidate") from None
    cache_key = json.dumps([body.text, config], sort_keys=True)

    def infer():
        with torch.inference_mode():
            reference = config["reference"]
            if reference not in conditioning:
                model.prepare_conditionals(reference, exaggeration=config["exaggeration"])
                conditioning[reference] = model.conds
            model.conds = conditioning[reference]
            torch.manual_seed(config["seed"])
            audio = model.generate(
                body.text,
                language_id="en",
                **{
                    key: config[key]
                    for key in (
                        "exaggeration",
                        "cfg_weight",
                        "temperature",
                        "repetition_penalty",
                        "min_p",
                        "top_p",
                    )
                },
            )
        buffer = io.BytesIO()
        sf.write(buffer, audio.squeeze().cpu().numpy(), model.sr, format="WAV")
        return buffer.getvalue()

    cached = speech_cache.get(cache_key)
    if cached:
        return Response(
            cached, media_type="audio/wav", headers={"X-TTS-Milliseconds": "0", "X-Audio-Cache": "hit"}
        )
    speech_stats["pending"] += 1
    try:
        async with lock:
            if await request.is_disconnected():
                speech_stats["abandoned"] += 1
                raise HTTPException(499, "Playback was cancelled before synthesis")
            data = speech_cache.get(cache_key)
            if data is None:
                data = await asyncio.to_thread(infer)
                speech_cache.put(cache_key, data)
                speech_stats["completed"] += 1
    finally:
        speech_stats["pending"] -= 1
    return Response(
        data,
        media_type="audio/wav",
        headers={"X-TTS-Milliseconds": str(round((time.monotonic() - started) * 1000))},
    )
