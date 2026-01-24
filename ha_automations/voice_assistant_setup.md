# Voice PE Integration Setup Guide

This guide sets up the Home Assistant Voice PE to use your Brain Gateway for ADHD support.

## Architecture

```
Voice PE (wake word: "Hey Jarvis" or custom)
         │
         ▼
Home Assistant Voice Pipeline
         │
         ├─── STT ──► Whisper on Uranus (10.0.0.173:8003)
         │
         ├─── Conversation Agent ──► Brain Gateway (10.0.0.186:8888)
         │                                    │
         │                                    ▼
         │                           Nemotron-8B on Saturn
         │
         └─── TTS ──► Qwen3-TTS/Jessica on Uranus (10.0.0.173:8002)
                              │
                              ▼
                      Voice PE Speaker
```

## Step 1: Add Configuration

Copy the contents of `configuration_additions.yaml` to your Home Assistant `configuration.yaml`.

```bash
# On your Home Assistant machine
nano /config/configuration.yaml
# Paste the REST commands and sensors
```

Restart Home Assistant after adding the configuration.

## Step 2: Install OpenAI Conversation Integration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "OpenAI Conversation"
3. Configure with these settings:
   - **API Key**: `dummy` (not used but required)
   - **Base URL**: `http://10.0.0.186:8888/v1`

4. After adding, click **Configure** on the integration:
   - **Model**: `nemotron`
   - **Max tokens**: `200`
   - **System prompt**:
     ```
     You are a supportive ADHD assistant named Brain. Be warm, encouraging,
     and concise. Help with medication reminders, motivation, and getting
     out of spirals. Keep responses brief for voice. Speak naturally.
     ```

## Step 3: Configure Speech-to-Text

### Option A: OpenAI Speech (Recommended)

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "OpenAI"
3. Configure:
   - **API Key**: `dummy`
   - **Base URL**: `http://10.0.0.173:8003/v1`

### Option B: Whisper Add-on + Custom Server

If Option A doesn't work, use the Whisper add-on with a custom configuration.

## Step 4: Configure Text-to-Speech

### Create a Custom TTS Notification

Since Qwen3-TTS isn't natively supported, create an automation for TTS:

Add to `automations.yaml`:

```yaml
alias: "Brain Gateway TTS"
description: "Play TTS via Qwen3-TTS with Jessica's voice"
mode: queued
trigger:
  - platform: event
    event_type: brain_tts_request
action:
  - service: rest_command.brain_tts
    data:
      text: "{{ trigger.event.data.text }}"
      speaker: "{{ trigger.event.data.speaker | default('media_player.voice_pe') }}"
```

Add to `configuration.yaml`:

```yaml
rest_command:
  brain_tts:
    url: "http://10.0.0.173:8002/tts"
    method: POST
    headers:
      Content-Type: "application/json"
    payload: >
      {
        "text": "{{ text }}",
        "voice": "jessica",
        "output_path": "/tmp/tts_output.wav"
      }
    content_type: "application/json"
```

## Step 5: Create Voice Pipeline

1. Go to **Settings** → **Voice assistants**
2. Click **Add assistant**
3. Configure:
   - **Name**: `Brain Gateway`
   - **Language**: English
   - **Conversation agent**: OpenAI Conversation (configured in Step 2)
   - **Speech-to-text**: OpenAI Whisper (configured in Step 3)
   - **Text-to-speech**: Select available option (see Alternative TTS below)
   - **Wake word**: Select your preferred wake word

4. **Important**: Set this as the default assistant for your Voice PE device

## Step 6: Configure Voice PE Device

1. Go to **Settings** → **Devices & Services**
2. Find your Voice PE device
3. Click **Configure**
4. Set **Voice Assistant** to "Brain Gateway"

## Alternative TTS: Piper with Custom Voice

If Qwen3-TTS integration is complex, use Piper TTS:

1. Install the Piper add-on
2. Configure it to use a warm female voice
3. Use Piper as the TTS in your voice pipeline

## Testing the Pipeline

### Voice Test
1. Say your wake word to Voice PE
2. Ask: "Can you remind me to take my medication?"
3. Brain Gateway should respond with an encouraging message

### Manual API Test
```bash
# Test STT
curl -X POST http://10.0.0.173:8003/v1/audio/transcriptions \
  -F "file=@test.wav" \
  -F "model=whisper-1"

# Test Brain Gateway
curl -X POST http://10.0.0.186:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nemotron",
    "messages": [{"role": "user", "content": "I am feeling unmotivated"}]
  }'

# Test TTS
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "You got this! One small step at a time.", "voice": "jessica"}'
```

## Troubleshooting

### Voice PE not responding
- Check that Voice PE is connected to Home Assistant
- Verify the wake word is configured
- Check Home Assistant logs for errors

### STT not working
- Test Whisper directly: `curl http://10.0.0.173:8003/health`
- Check Uranus GPU 1 status
- Review systemd logs: `ssh uranus 'journalctl -u whisper-stt -n 50'`

### Brain Gateway not responding
- Test health: `curl http://10.0.0.186:8888/health`
- Check orchestrator logs: `docker logs brain-orchestrator`
- Verify Nemotron is running on Saturn

### TTS not working
- Test TTS directly: `curl http://10.0.0.173:8002/health`
- Check Uranus GPU 0 status
- Review systemd logs: `ssh uranus 'journalctl -u qwen-tts -n 50'`

## ADHD-Friendly Voice Commands

Once set up, try these:

- "Hey Brain, remind me to take my Adderall"
- "I'm stuck in a spiral, help me get out"
- "I need motivation to do the dishes"
- "What's on my schedule today?"
- "Help me break down cleaning my room"
