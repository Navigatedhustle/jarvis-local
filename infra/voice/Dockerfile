FROM nvcr.io/nvidia/pytorch:25.11-py3 AS stt
RUN pip install 'transformers>=5.8,<6' 'fastapi>=0.115' uvicorn python-multipart soundfile librosa scipy accelerate
WORKDIR /app
COPY src/jarvis/speech_service.py /app/speech_service.py
COPY src/jarvis/speech_cache.py /app/speech_cache.py
COPY src/jarvis/voice_profiles.py /app/voice_profiles.py
ENV HF_HOME=/models/cache HF_HUB_DISABLE_IMPLICIT_TOKEN=1 JARVIS_SPEECH_ROLE=stt
CMD ["uvicorn", "speech_service:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM stt AS tts
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng && rm -rf /var/lib/apt/lists/*
RUN espeak-ng -v en-gb -p 38 -s 145 -w /app/original-british-reference.wav 'Good morning. Your schedule is ready, and the important work is in hand. Let us keep this precise. There is rather more value in a clear decision than in another meeting.'
RUN git clone --depth 1 --branch v2.9.1 https://github.com/pytorch/audio.git /opt/torchaudio && cd /opt/torchaudio && USE_CUDA=0 BUILD_SOX=0 BUILD_RNNT=0 pip install --no-build-isolation --no-deps .
RUN git clone https://github.com/resemble-ai/chatterbox.git /opt/chatterbox && cd /opt/chatterbox && git checkout 5de7a54aa4e5e2baadb0182dde554908b48b85c2
COPY scripts/install_chatterbox.py /tmp/install_chatterbox.py
RUN python /tmp/install_chatterbox.py
ENV JARVIS_SPEECH_ROLE=tts
ENV JARVIS_VOICE_REFERENCE=/app/original-british-reference.wav
