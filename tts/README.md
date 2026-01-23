# Qwen3-TTS Server

FastAPI server for Qwen3-TTS, providing natural voice synthesis for Brain Gateway.

**Target deployment:** Uranus (10.0.0.173) with RTX 5080 GPUs

## Features

- **49 preset voices** with emotion/style control
- **Voice cloning** from 3-second audio samples
- **Voice design** from text descriptions
- **OpenAI-compatible endpoint** (`/v1/audio/speech`)
- **Low latency** with FlashAttention 2

## Quick Start (Uranus)

### 1. Install Dependencies

```bash
# SSH to Uranus
ssh nadim@10.0.0.173

# Create conda environment
conda create -n qwen3-tts python=3.12 -y
conda activate qwen3-tts

# Install requirements
cd /opt/voyager/gateway_mvp/tts
pip install -r requirements.txt

# Install FlashAttention 2 (recommended)
pip install -U flash-attn --no-build-isolation
```

### 2. Download Model

```bash
# Option A: Hugging Face
huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --local-dir ~/models/Qwen3-TTS-1.7B-CustomVoice

# Option B: ModelScope (faster in China)
modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --local_dir ~/models/Qwen3-TTS-1.7B-CustomVoice
```

### 3. Run Server

```bash
# Direct run
cd /opt/voyager/gateway_mvp/tts
QWEN_TTS_MODEL=~/models/Qwen3-TTS-1.7B-CustomVoice python server.py

# Or with uvicorn
QWEN_TTS_MODEL=~/models/Qwen3-TTS-1.7B-CustomVoice \
  uvicorn server:app --host 0.0.0.0 --port 8002
```

### 4. Install as Service

```bash
sudo cp qwen-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable qwen-tts
sudo systemctl start qwen-tts
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_TTS_MODEL` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | Model path or HF repo |
| `QWEN_TTS_DEVICE` | `cuda:0` | GPU device |
| `QWEN_TTS_PORT` | `8002` | Server port |
| `QWEN_TTS_DTYPE` | `bfloat16` | Model dtype |
| `QWEN_TTS_FLASH_ATTN` | `true` | Use FlashAttention 2 |

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

### Voice Cloning
```bash
curl -X POST http://10.0.0.173:8002/tts/clone \
  -F "text=Hello from my cloned voice!" \
  -F "ref_text=This is the transcript of my reference audio." \
  -F "ref_audio=@my_voice_sample.wav" \
  --output cloned.wav
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

### OpenAI-Compatible
```bash
curl -X POST http://10.0.0.173:8002/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello world!", "voice": "Olivia"}' \
  --output speech.wav
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

### Option 1: Direct Call from Orchestrator

Add to `orchestrator.py`:

```python
import httpx

QWEN_TTS_URL = "http://10.0.0.173:8002"

async def synthesize_speech(text: str, voice: str = "Ethan") -> bytes:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{QWEN_TTS_URL}/tts",
            json={"text": text, "voice": voice, "emotion": "warm and helpful"}
        )
        return response.content
```

### Option 2: Home Assistant Wyoming Protocol

For integration with HA voice pipeline, you'd need to implement the Wyoming protocol wrapper. This is more complex but allows using Qwen3-TTS as a drop-in Piper replacement.

## Troubleshooting

### CUDA Out of Memory
Try the 0.6B model instead:
```bash
QWEN_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice python server.py
```

### FlashAttention Not Working
Disable it:
```bash
QWEN_TTS_FLASH_ATTN=false python server.py
```

### Slow First Request
First request loads the model. Subsequent requests are fast (~200ms).

## Hardware Requirements

| Model | VRAM | RAM |
|-------|------|-----|
| 0.6B | ~2GB | 8GB |
| 1.7B | ~4GB | 16GB |

Uranus (2x RTX 5080, 32GB VRAM) handles the 1.7B model easily.
