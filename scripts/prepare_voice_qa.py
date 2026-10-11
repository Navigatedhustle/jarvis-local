"""Create a clearly synthetic workday question with the existing original local voice."""

import audioop
import io
import wave
from pathlib import Path

import httpx


def main():
    response = httpx.post(
        "http://127.0.0.1:8186/synthesize",
        json={"text": "Jarvis, what needs my attention today?"},
        timeout=120,
    )
    response.raise_for_status()
    with wave.open(io.BytesIO(response.content), "rb") as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        pcm, _ = audioop.ratecv(
            source.readframes(source.getnframes()), 2, 1, source.getframerate(), 16000, None
        )
    with wave.open(str(Path("data/stt-workday-sample.wav")), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(pcm)
    print("Synthetic HP workday voice input prepared locally; no user recording.")


if __name__ == "__main__":
    main()
