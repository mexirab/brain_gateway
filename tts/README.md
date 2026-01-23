# Voice Pipeline (TTS + STT)

FastAPI servers for Qwen3-TTS and Whisper STT, running on Uranus (10.0.0.173).

**Deployment:**
- GPU 0 (cuda:0): Qwen3-TTS on port 8002
- GPU 1 (cuda:1): Whisper STT on port 8003

## TTS Features

- **49 preset voices** with emotion/style control
- **Voice cloning** from audio samples (requires Base model, not CustomVoice)
- **Auto-load voices** from `~/tts-voices/voices.json` on startup
- **OpenAI-compatible endpoint** (`/v1/audio/speech`)
- **Jessica McCabe voice** pre-configured for ADHD-friendly announcements

## STT Features

- **Whisper-based** speech recognition
- **OpenAI-compatible API** (`/v1/audio/transcriptions`)
- **Configurable model size** (tiny, base, small, medium, large)

## Quick Start (Uranus)

### 1. Install Dependencies

```bash
# SSH to Uranus
ssh nadim@10.0.0.173

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
  "jessica": {
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus on anything I was not excited about was like trying to nail jello to the wall.",
    "description": "Jessica McCabe - warm, energetic ADHD advocate"
  }
}
EOF
```

### 4. Install Services

```bash
# Copy service files
sudo cp qwen-tts.service /etc/systemd/system/
sudo cp whisper-stt.service /etc/systemd/system/

# Copy server files to home directory
cp server.py ~/server.py
cp stt_server.py ~/stt_server.py

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable qwen-tts whisper-stt
sudo systemctl start qwen-tts whisper-stt
```

## Configuration

### TTS Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_TTS_MODEL` | (required) | Model path (use Base for cloning) |
| `QWEN_TTS_DEVICE` | `cuda:0` | GPU device |
| `QWEN_TTS_PORT` | `8002` | Server port |
| `QWEN_TTS_DTYPE` | `bfloat16` | Model dtype |
| `QWEN_TTS_FLASH_ATTN` | `false` | Use FlashAttention 2 (disabled by default) |

### STT Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `base` | Model size (tiny, base, small, medium, large) |
| `WHISPER_DEVICE` | `cuda:1` | GPU device (use GPU 1 alongside TTS on GPU 0) |
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
    "text": "Hey Nadim, your morning meds are due.",
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
    "name": "jessica",
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus...",
    "description": "Jessica McCabe voice"
  }'
```

### Use Cloned Voice
```bash
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning!", "voice": "jessica"}' \
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

### Morning Briefing Endpoint

The orchestrator has a `/api/briefing/morning` endpoint that:
1. Searches RAG for morning routine/meds info
2. Generates personalized briefing via Nemotron
3. Synthesizes audio with Jessica's voice
4. Plays on HA speaker via `media_player.play_media`

```bash
curl -X POST http://localhost:8888/api/briefing/morning \
  -H "Content-Type: application/json" \
  -d '{"generate_tts": true, "play_on": "media_player.kitchen_display"}'
```

### Open WebUI Integration

Configure Open WebUI to use TTS/STT in `docker-compose.yml`:

```yaml
environment:
  # TTS
  - AUDIO_TTS_ENGINE=openai
  - AUDIO_TTS_OPENAI_API_BASE_URL=http://10.0.0.173:8002/v1
  - AUDIO_TTS_OPENAI_API_KEY=local
  - AUDIO_TTS_VOICE=jessica
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
sudo systemctl status qwen-tts whisper-stt

# View logs
journalctl -u qwen-tts -f
journalctl -u whisper-stt -f
```

## Hardware Requirements

| Service | Model | VRAM | GPU |
|---------|-------|------|-----|
| TTS | Qwen3-TTS-1.7B-Base | ~4GB | cuda:0 |
| STT | Whisper base | ~1GB | cuda:1 |

Uranus (2x RTX 5080, 32GB total VRAM) handles both services easily with room to spare.
