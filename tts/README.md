# Voice Pipeline (TTS + STT)

FastAPI servers for Qwen3-TTS and Parakeet STT, running on Helios (10.0.0.195) as systemd services.

**Deployment:**
- Both services pinned to GPU1 (RTX PRO 5000 Blackwell, 48GB) alongside the primary Qwen3.5-27B LLM
- Qwen3-TTS on port 8002 (`QWEN_TTS_DEVICE=cuda:1`)
- Parakeet STT on port 8003 (`PARAKEET_DEVICE=cuda:0` with `CUDA_VISIBLE_DEVICES=1`) — replaced the old Whisper HTTP STT server on 2026-04-26 (same port, same OpenAI-compatible API). See `README_PARAKEET.md`.

Wyoming bridges in Docker on Helios wrap these for Home Assistant: `wyoming-whisper` (:10300, still `wyoming-faster-whisper` — the HA voice-pipeline STT, independent of the HTTP STT server above) and `wyoming-jessica-tts` (:10301).

## TTS Features

- **49 preset voices** with emotion/style control
- **Voice cloning** from audio samples (requires Base model, not CustomVoice)
- **Auto-load voices** from `~/tts-voices/voices.json` on startup
- **OpenAI-compatible endpoint** (`/v1/audio/speech`)
- **Default voice** pre-configured for ADHD-friendly announcements

## STT Features

- **Whisper-based** speech recognition
- **OpenAI-compatible API** (`/v1/audio/transcriptions`)
- **Configurable model size** (tiny, base, small, medium, large)

## Quick Start (Helios)

### 1. Install Dependencies

```bash
# SSH to Helios
ssh labadmin@10.0.0.195

# Create virtual environment
python -m venv ~/qwen-tts-env
source ~/qwen-tts-env/bin/activate

# Install requirements
pip install torch torchaudio transformers fastapi uvicorn python-multipart pydub soundfile openai-whisper

# FlashAttention is optional - disable if not working
# pip install -U flash-attn --no-build-isolation
```

### 2. Download Model

**IMPORTANT:** Use the Base model for voice cloning (CustomVoice doesn't support cloning):

```bash
huggingface-cli download Qwen/Qwen3-TTS-1.7B-Base \
  --local-dir ~/models/Qwen3-TTS-1.7B-Base
```

### 3. Set Up Voice Cloning

Create voice config directory and add voice samples:

```bash
mkdir -p ~/tts-voices

# Create voices.json with your cloned voices
cat > ~/tts-voices/voices.json << 'EOF'
{
  "myvoice": {
    "ref_audio": "/home/youruser/tts-voices/myvoice_sample.wav",
    "ref_text": "A transcript of exactly what is spoken in the reference audio clip.",
    "description": "Warm, energetic voice"
  }
}
EOF
```

### 4. Install Services

```bash
# Copy service files
sudo cp qwen-tts.service /etc/systemd/system/
sudo cp parakeet-stt.service /etc/systemd/system/

# Copy server files to home directory
cp server.py ~/server.py
cp stt_server_parakeet.py ~/stt_server_parakeet.py

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable qwen-tts parakeet-stt
sudo systemctl start qwen-tts parakeet-stt
```

## Configuration

### TTS Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_TTS_MODEL` | (required) | Model path (use Base for cloning) |
| `QWEN_TTS_DEVICE` | `cuda:1` | GPU device (GPU1 RTX PRO 5000 Blackwell, shared with Qwen3.5-27B + Parakeet STT) |
| `QWEN_TTS_PORT` | `8002` | Server port |
| `QWEN_TTS_DTYPE` | `bfloat16` | Model dtype |
| `QWEN_TTS_FLASH_ATTN` | `false` | Use FlashAttention 2 (disabled by default) |

### STT Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `base` | Model size (tiny, base, small, medium, large) |
| `WHISPER_DEVICE` | `cuda:1` | GPU device (GPU1 RTX PRO 5000 Blackwell, shared with Qwen3.5-27B + Qwen3-TTS) |
| `WHISPER_PORT` | `8003` | Server port |

## API Endpoints

### Health Check
```bash
curl http://10.0.0.173:8002/health
```

### List Voices
```bash
curl http://10.0.0.173:8002/voices
```

### Text-to-Speech
```bash
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hey there, your morning meds are due.",
    "voice": "Ethan",
    "emotion": "warm and friendly"
  }' \
  --output speech.wav
```

### Voice Cloning (One-Time Setup)
```bash
# Load a cloned voice (saves to voices.json automatically)
curl -X POST http://10.0.0.173:8002/voices/load \
  -H "Content-Type: application/json" \
  -d '{
    "name": "myvoice",
    "ref_audio": "/home/youruser/tts-voices/myvoice_sample.wav",
    "ref_text": "A transcript of exactly what is spoken in the reference clip...",
    "description": "Warm, energetic voice"
  }'
```

### Use Cloned Voice
```bash
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning!", "voice": "myvoice"}' \
  --output speech.wav
```

### Voice Design
```bash
curl -X POST http://10.0.0.173:8002/tts/design \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Good morning!",
    "voice_description": "warm female voice with slight British accent, cheerful tone"
  }' \
  --output designed.wav
```

### OpenAI-Compatible TTS
```bash
curl -X POST http://10.0.0.173:8002/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello world!", "voice": "Olivia"}' \
  --output speech.wav
```

## STT API Endpoints

### Transcribe Audio
```bash
curl -X POST http://10.0.0.173:8003/v1/audio/transcriptions \
  -F "file=@audio.wav" \
  -F "model=whisper-1"
```

### Health Check
```bash
curl http://10.0.0.173:8003/health
```

## Available Voices

| Voice | Description | Languages |
|-------|-------------|-----------|
| Ethan | Male, warm and professional | EN, ZH |
| Olivia | Female, clear and friendly | EN, ZH |
| Lucas | Male, energetic and youthful | EN, ZH |
| Sophia | Female, calm and reassuring | EN, ZH |
| Alexander | Male, deep and authoritative | EN, ZH |
| Emma | Female, bright and expressive | EN, ZH |
| Benjamin | Male, mature and thoughtful | EN, ZH |
| Isabella | Female, soft and gentle | EN, ZH |
| William | Male, confident and clear | EN, ZH |
| Chelsie | Female, sweet and lively | ZH, EN |
| Cherry | Female, warm and natural | ZH, EN |
| Serena | Female, elegant and refined | ZH, EN |
| Vivian | Female, expressive and dynamic | ZH, EN |
| Daniel | Male, steady and trustworthy | ZH, EN |
| James | Male, casual and approachable | ZH, EN |
| Kevin | Male, young and enthusiastic | ZH, EN |

## Integration with Brain Gateway

### Morning Briefing Script

A shell script (`scripts/morning_briefing.sh`) handles morning briefings:
1. Searches RAG for morning routine/meds info
2. Generates personalized briefing via Nemotron
3. Synthesizes audio with the configured voice
4. Plays on HA speaker via `media_player.play_media`

```bash
/opt/gateway_mvp/scripts/morning_briefing.sh
```

### Open WebUI Integration

Configure Open WebUI to use TTS/STT in `docker-compose.yml`:

```yaml
environment:
  # TTS
  - AUDIO_TTS_ENGINE=openai
  - AUDIO_TTS_OPENAI_API_BASE_URL=http://10.0.0.173:8002/v1
  - AUDIO_TTS_OPENAI_API_KEY=local
  - AUDIO_TTS_VOICE=default
  # STT
  - AUDIO_STT_ENGINE=openai
  - AUDIO_STT_OPENAI_API_BASE_URL=http://10.0.0.173:8003/v1
  - AUDIO_STT_OPENAI_API_KEY=local
  - AUDIO_STT_MODEL=whisper-1
```

## Troubleshooting

### Voice cloning fails with "model does not support generate_voice_clone"
You're using the CustomVoice model. Switch to Base model:
```bash
QWEN_TTS_MODEL=~/models/Qwen3-TTS-1.7B-Base
```

### FlashAttention ImportError
Disable FlashAttention in the service config:
```bash
QWEN_TTS_FLASH_ATTN=false
```

### Slow First Request
First request for a cloned voice generates the voice prompt (cached afterward).
Preset voices are faster on first use.

### Service Status
```bash
# Check both services
sudo systemctl status qwen-tts parakeet-stt

# View logs
journalctl -u qwen-tts -f
journalctl -u parakeet-stt -f
```

## Hardware Requirements

| Service | Model | VRAM | GPU |
|---------|-------|------|-----|
| TTS | Qwen3-TTS-1.7B-Base | ~4GB | cuda:0 |
| STT | Whisper base | ~1GB | cuda:1 |

Uranus (2x RTX 5080, 32GB total VRAM) handles both services easily with room to spare.
