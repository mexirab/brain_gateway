# Qwen3-TTS server image (Phase 1 containerization).
#
# Ubuntu 24.04 base = Python 3.12, matching the known-working host venv
# (qwen-tts-env). torch is installed from the cu128 wheel index: Helios's
# Blackwell GPUs require cu128, and older 24GB-class cards (3090/4090) run
# cu128 fine too. All other deps are exact-pinned in requirements.txt
# (generated from the host venv) for reproducible builds.
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH

WORKDIR /app

# ffmpeg + sox: required by the audio deps (pydub / pysox). libsndfile1:
# required by soundfile + librosa. A venv sidesteps Ubuntu 24.04's PEP 668
# externally-managed-environment block on the system Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv curl ffmpeg sox libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /venv

# torch FIRST, from the cu128 wheel index — pulls the matching nvidia-*-cu12
# and triton builds. The default PyPI torch wheel targets a newer CUDA than
# Helios's driver supports and fails at runtime ("NVIDIA driver too old").
RUN pip install --no-cache-dir torch==2.10.0 torchaudio==2.10.0 \
        --index-url https://download.pytorch.org/whl/cu128

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Model defaults to the HuggingFace repo id so it downloads into the mounted
# model-hf-cache volume on first run — no baked-in multi-GB layer, no host path.
ENV QWEN_TTS_MODEL=Qwen/Qwen3-TTS-1.7B-Base \
    QWEN_TTS_PORT=8002

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

CMD ["python3", "server.py"]
