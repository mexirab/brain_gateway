# Parakeet STT — deploy & rollback runbook

`stt_server_parakeet.py` is a drop-in replacement for the Whisper STT server
(`stt_server.py`) that uses NVIDIA Parakeet TDT V3 via NeMo. It binds the same
port (`8003`) and exposes the same three endpoints, so Open WebUI and the
Wyoming bridge need no config changes.

**Expected wins:** ~10× faster (RTFx >2000), lower WER (6.32% vs Whisper
medium's 7.44%), sub-200ms streaming. Frees real VRAM headroom on Helios GPU1
(RTX PRO 5000) which is currently shared with the primary LLM and TTS.

**Trade-off:** Parakeet is English-only. ATOM Echo and Open WebUI usage in this
deployment is English, so this is acceptable.

**Cutover gap:** stopping Whisper and waiting for Parakeet's lifespan to bind
port 8003 takes up to a minute on a warm cache (longer on cold). ATOM Echo
retries; Open WebUI users who hit record during the window will see a transient
error.

## One-time install (Helios host)

```bash
# 1. Stage files into the locations the systemd unit expects
cp /opt/helios/gateway_mvp/tts/stt_server_parakeet.py /home/labadmin/stt_server_parakeet.py
sudo cp /opt/helios/gateway_mvp/tts/parakeet-stt.service /etc/systemd/system/parakeet-stt.service

# 2. Verify ffmpeg is on the host (already required by the Whisper server)
which ffmpeg || sudo apt install -y ffmpeg

# 3. Build a dedicated venv (keeps NeMo's heavy deps out of qwen-tts-env).
# Note: python3.12-venv is held back on Helios (nvidia compat reasons), so
# use virtualenv instead of the stdlib venv module.
sudo apt install -y python3-virtualenv
python3 -m virtualenv /home/labadmin/parakeet-env
/home/labadmin/parakeet-env/bin/pip install --upgrade pip

# 3a. Install torch FIRST, pinned to a cu121 wheel, from the PyTorch index
# as the PRIMARY source (--index-url, not --extra-index-url). Helios's NVIDIA
# driver maxes out at CUDA 12.8; the default PyPI torch wheel is built against
# CUDA 13 and will fail with "NVIDIA driver too old" at runtime.
/home/labadmin/parakeet-env/bin/pip install \
    torch==2.5.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cu121

# 3b. Install the rest of the stack. NeMo has a torch dep, but since torch
# is already present at 2.5.1 it won't upgrade.
/home/labadmin/parakeet-env/bin/pip install \
    "nemo_toolkit[asr]" \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    soundfile

# 3c. Sanity check: torch must report cu121 and see the GPUs before continuing.
/home/labadmin/parakeet-env/bin/python -c \
    "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.device_count())"
# Expected: 2.5.1+cu121 12.1 True 2

# After validating the cutover, pin the rest of the versions you tested into a
# requirements-parakeet.txt next to this README so reinstalls are reproducible.

# 4. Pre-download the Parakeet weights into the HF cache
/home/labadmin/parakeet-env/bin/python -c \
    "import nemo.collections.asr as nemo_asr; nemo_asr.models.ASRModel.from_pretrained('nvidia/parakeet-tdt-0.6b-v3')"
```

## Cutover

```bash
sudo systemctl daemon-reload
sudo systemctl stop whisper-stt
sudo systemctl start parakeet-stt
# Only enable after the smoke test below passes:
sudo systemctl enable parakeet-stt
```

## Smoke test

```bash
# 1. Service is up and model loaded — REQUIRED: confirm model_loaded:true
curl -s http://localhost:8003/health | jq
# Expected: {"status":"healthy","model":"nvidia/parakeet-tdt-0.6b-v3",
#            "device":"cuda:1","model_loaded":true}

# 2. End-to-end transcription
curl -s -X POST http://localhost:8003/v1/audio/transcriptions \
     -F file=@/path/to/sample.wav \
     -F model=whisper-1 | jq

# 3. Wyoming bridge can still reach the server
docker exec wyoming-whisper python -c \
    "import socket; socket.create_connection(('10.0.0.195', 8003), 5); print('ok')"

# 4. Tail logs for any tracebacks
journalctl -u parakeet-stt -n 100 --no-pager
```

Then test interactively:
- Trigger an ATOM Echo voice command via Home Assistant.
- Record + submit a clip in Open WebUI.

## Rollback

Whisper's venv, model files, and systemd unit are untouched, so rollback is
instantaneous and requires no rebuild:

```bash
sudo systemctl stop parakeet-stt
sudo systemctl start whisper-stt
```

Optionally disable Parakeet from auto-start while you investigate:

```bash
sudo systemctl disable parakeet-stt
```

## Files

| Path | Purpose |
|------|---------|
| `tts/stt_server_parakeet.py` | FastAPI server (NeMo Parakeet backend) |
| `tts/parakeet-stt.service` | systemd unit (cuda:1, port 8003) |
| `tts/stt_server.py` | Whisper server, kept verbatim for rollback |
| `tts/whisper-stt.service` | Whisper unit, kept verbatim for rollback |
