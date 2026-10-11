"""Generate an original synthetic reference using Apache-2.0 Kokoro weights, on CPU.

This prepares conditioning audio only. Production TTS remains Chatterbox V3.
No person's recording or celebrity prompt is used.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

root = Path("/models/voices")
root.mkdir(parents=True, exist_ok=True)
text = (
    "Good evening, sir. You have three items worth your attention. "
    "Your morning meeting needs preparation, the project notes are ready, "
    "and there is one decision I would rather we made before tomorrow. "
    "Everything else can wait. A little perspective saves a remarkable amount of time."
)
pipeline = KPipeline(lang_code="b", repo_id="hexgrad/Kokoro-82M", device="cpu")
voice = pipeline.load_voice("bm_george") * 0.65 + pipeline.load_voice("bm_fable") * 0.35
audio = np.concatenate([result.audio.numpy() for result in pipeline(text, voice=voice, speed=0.98)])
target = root / "original-british-chief-of-staff.wav"
sf.write(target, audio, 24000, subtype="PCM_16")
metadata = {
    "id": "original-british-chief-of-staff",
    "kind": "original synthetic blend",
    "generator": "hexgrad/Kokoro-82M",
    "license": "Apache-2.0",
    "source": "https://huggingface.co/hexgrad/Kokoro-82M",
    "model_card_training_provenance": "Permissive/non-copyrighted audio and synthetic audio; see source model card.",
    "voices": {"bm_george": 0.65, "bm_fable": 0.35},
    "speed": 0.98,
    "transcript": text,
    "sample_rate": 24000,
    "duration_seconds": len(audio) / 24000,
    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    "impersonation": False,
}
target.with_suffix(".json").write_text(json.dumps(metadata, indent=2))
print(
    json.dumps(
        {"reference": str(target), "duration": metadata["duration_seconds"], "sha256": metadata["sha256"]}
    )
)
