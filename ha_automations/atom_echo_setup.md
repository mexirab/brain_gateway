# ATOM Echo Voice Assistant Setup Guide

Complete setup guide for M5Stack ATOM Echo with custom "Hey Jess" wake word.

## Overview

```
┌─ ALWAYS-ON ──────────────────────────────────────────┐
│                                                       │
│  ATOM Echo: "Hey Jess" wake word (microWakeWord)     │
│  └─ On-device detection, sends audio to HA           │
│                                                       │
│  Saturn: Nemotron-8B (~16GB VRAM)                    │
│  └─ The brain - handles 95%+ of daily needs          │
│                                                       │
│  Uranus GPU 0: TTS/Jessica (~4GB VRAM)               │
│  Uranus GPU 1: Whisper STT (~1GB VRAM)               │
│                                                       │
│  Voyager: Orchestrator + Home Assistant              │
│                                                       │
└───────────────────────────────────────────────────────┘

┌─ OFF BY DEFAULT ─────────────────────────────────────┐
│  Helios: 120B Expert (auto-starts when needed)       │
└───────────────────────────────────────────────────────┘
```

## Voice Pipeline

```
"Hey Jess" → ATOM Echo (microWakeWord on-device)
                    │
                    ▼
         Home Assistant (Wyoming)
                    │
                    ▼
         Whisper STT (Uranus:8003)
                    │
                    ▼
         Brain Gateway Orchestrator (:8888)
                    │
                    ▼
         Nemotron-8B (Saturn:8001)
                    │
                    ▼
         Jessica TTS (Uranus:8002)
                    │
                    ▼
         Google Cast Speaker (room-based routing)
```

**Note:** ATOM Echo serves as wake word detector and microphone only.
TTS responses are routed to Google speakers for better audio quality.

---

## Step 1: Flash ATOM Echo with ESPHome

### 1.1 Web-Based Flashing (Easiest)

1. Connect ATOM Echo to your computer via USB-C
2. Open Chrome/Edge browser (requires WebSerial support)
3. Go to: https://esphome.io/projects/
4. Select "M5Stack ATOM Echo"
5. Click "Connect" and select the USB serial port
6. Click "Install" to flash the base firmware
7. When prompted, enter your WiFi credentials

### 1.2 Verify Device Connection

After flashing and WiFi setup:
1. Check your router's DHCP list for the new device
2. Note the IP address assigned to `atom-echo`
3. Verify you can ping the device

### 1.3 Adopt in ESPHome Dashboard

1. Open ESPHome Dashboard (HA add-on or standalone)
2. The device should appear with "Adopt" button
3. Click "Adopt" and give it a name (e.g., `atom-echo-jess`)
4. Wait for initial compilation

---

## Step 2: Train "Hey Jess" Wake Word

See `hey_jess_training.md` for detailed instructions.

### Quick Summary

**Google Colab (Free, ~1 hour):**
1. Open: https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
2. Set `target_word = "hey jess"`
3. Run all cells
4. Download `hey_jess.tflite`

**Docker with GPU (Faster):**
```bash
docker run --rm -it --gpus all -p 8888:8888 \
  -v $(pwd):/data ghcr.io/tatertotterson/microwakeword:latest
```

---

## Step 2.5: Host Model Files via HTTP (Required)

**Important:** ESPHome's micro_wake_word requires model files to be accessible via HTTP URL, not local file paths.

The model-server nginx container on Voyager serves the wake word files at `http://10.0.0.186:8080/`.

### Files Served
- `http://10.0.0.186:8080/hey_jess.json` - Wake word manifest
- `http://10.0.0.186:8080/hey_jess.tflite` - Trained model

### Verify Model Server
```bash
# Check JSON manifest
curl http://10.0.0.186:8080/hey_jess.json

# Check model file
curl -I http://10.0.0.186:8080/hey_jess.tflite
```

### If Model Server is Down
```bash
# On Voyager, restart the container
cd /opt/voyager/gateway_mvp
docker compose up -d model-server
```

---

## Step 3: Deploy ESPHome Configuration

### 3.1 Create Secrets File

Create `secrets.yaml` in your ESPHome config directory:

```yaml
# WiFi
wifi_ssid: "YourWiFiName"
wifi_password: "YourWiFiPassword"

# ESPHome API (generate a random key)
api_encryption_key: "your-base64-key-here"

# OTA password (choose something secure)
ota_password: "your-ota-password"
```

To generate an API key:
```bash
python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

### 3.2 Copy Configuration Files

Copy to your ESPHome config directory:
- `atom_echo.yaml` (device configuration)
- `hey_jess.tflite` (trained wake word model)

### 3.3 Compile and Upload

1. In ESPHome Dashboard, click on `atom-echo-jess`
2. Click "Install"
3. Choose "Wirelessly" (if device is on network) or "USB"
4. Wait for compilation and upload

---

## Step 4: Configure Home Assistant

### 4.1 Add Wyoming Integrations

Go to **Settings > Devices & Services > Add Integration > Wyoming Protocol**

Add two integrations:

**Speech-to-Text (Whisper):**
- Host: `10.0.0.173`
- Port: `8003`

**Text-to-Speech (Jessica/Qwen3):**
- Host: `10.0.0.173`
- Port: `8002`

### 4.2 Create Voice Assistant

Go to **Settings > Voice Assistants > Add Assistant**

Configuration:
- **Name:** Jess (Brain Gateway)
- **Language:** English
- **Conversation agent:** Home Assistant
- **Speech-to-text:** Whisper STT (10.0.0.173)
- **Text-to-speech:** Jessica TTS (10.0.0.173)

### 4.3 Configure ATOM Echo Device

1. Go to **Settings > Devices & Services**
2. Find **ESPHome** integration
3. Click on **atom-echo-jess** device
4. Click **Configure**
5. Select **Jess (Brain Gateway)** as the voice assistant

### 4.4 Add Configuration to Home Assistant

Add the following to your `configuration.yaml`:

```yaml
# Enable Wyoming protocol
wyoming:

# Include the ATOM Echo integration configs
# Copy rest_command, shell_command, input_select, etc. from atom_echo_integration.yaml
```

Add automations from `voice_conversation_automation.yaml` to your `automations.yaml`.

### 4.5 Configure Default Speaker

1. Go to **Settings > Devices & Services > Helpers**
2. Find **Jess Default Speaker** (input_select.jess_default_speaker)
3. Edit options to match your Google speaker entity IDs:
   - `media_player.living_room_speaker`
   - `media_player.bedroom_speaker`
   - `media_player.office_speaker`
   - etc.
4. Set the initial/default speaker

### 4.6 Room-Based Speaker Routing (Optional)

If you have multiple ATOM Echos, map each to its nearest Google speaker.
Edit `voice_conversation_automation.yaml` and update the `room_map`:

```yaml
{% set room_map = {
  'atom_echo_jess': states('input_select.jess_default_speaker'),
  'atom_echo_office': 'media_player.office_speaker',
  'atom_echo_bedroom': 'media_player.bedroom_speaker',
  'atom_echo_kitchen': 'media_player.kitchen_speaker',
  'atom_echo_living': 'media_player.living_room_speaker'
} %}
```

---

## Step 5: Verification Checklist

### 5.1 Wake Word Detection
- [ ] Say "Hey Jess" from 1 meter away
- [ ] LED turns cyan/blue when wake word detected
- [ ] Check ESPHome logs: `Wake word detected: Hey Jess`

### 5.2 Voice Pipeline
- [ ] After wake word, LED pulses blue (listening)
- [ ] Speak a command: "What time is it?"
- [ ] LED turns yellow (processing)
- [ ] LED turns green (TTS playing)
- [ ] Audio response plays

### 5.3 Brain Gateway Integration
- [ ] "Hey Jess, tell me about my medications"
- [ ] Response includes personalized information
- [ ] Check Brain Gateway logs for request

### 5.4 ADHD Support Features
- [ ] "Hey Jess, remind me to take my meds"
- [ ] "Hey Jess, help me get unstuck"
- [ ] "Hey Jess, I'm spiraling"
- [ ] Response is warm and supportive

### 5.5 Helios Expert Mode
- [ ] "Hey Jess, activate expert mode"
- [ ] Wait for confirmation (~2 minutes)
- [ ] "Hey Jess, [complex question]"
- [ ] Check that Helios is used for response
- [ ] "Hey Jess, save power" (deactivates Helios)

---

## Troubleshooting

### Wake Word Not Triggering

1. **Check model server is running**
   - `curl http://10.0.0.186:8080/hey_jess.json` should return JSON
   - If not: `docker compose up -d model-server` on Voyager

2. **Check probability_cutoff** in `atom_echo.yaml`
   - Try lowering to 0.3 if it never triggers
   - See `hey_jess_training.md` for tuning guidance

3. **Verify model is loaded**
   - Check ESPHome logs for model loading
   - ESPHome fetches from `http://10.0.0.186:8080/hey_jess.json`

4. **Microphone issues**
   - Press the button to test manual wake (bypasses wake word)
   - Check I2S pins are configured correctly

### Voice Commands Not Working

1. **Check Wyoming connections**
   - Verify Whisper STT is running: `curl http://10.0.0.173:8003/health`
   - Verify Jessica TTS is running: `curl http://10.0.0.173:8002/health`

2. **Check Home Assistant voice assistant**
   - Test STT/TTS separately in HA Developer Tools
   - Verify voice assistant is assigned to ATOM Echo

3. **Check Brain Gateway**
   - Verify orchestrator is running: `curl http://10.0.0.186:8888/health`
   - Check logs in Grafana or Docker

### Audio Issues

1. **No sound from Google speaker**
   - Verify the speaker entity ID in `input_select.jess_default_speaker`
   - Check Google Cast integration is working: test with Developer Tools > Services
   - Ensure speaker is powered on and connected to network

2. **Wrong speaker playing response**
   - Check room mapping in `voice_conversation_automation.yaml`
   - Verify ATOM Echo device ID matches the room_map keys
   - Set correct default speaker in HA Helpers

3. **TTS playing on ATOM Echo instead of Google speaker**
   - This is expected for the voice assistant's built-in response
   - HA automations override this and route to Google speakers
   - Check automations are loaded: Developer Tools > Services > reload automations

### False Positives

1. **Increase probability_cutoff** (e.g., 0.5 → 0.7)
2. **Increase sliding_window_size** (e.g., 5 → 7)
3. **Retrain model** with more negative examples

---

## File Reference

| File | Purpose |
|------|---------|
| `atom_echo.yaml` | ESPHome device configuration |
| `hey_jess.tflite` | Trained wake word model |
| `secrets.yaml` | WiFi/API credentials |
| `atom_echo_integration.yaml` | HA configuration additions |
| `voice_conversation_automation.yaml` | Voice command automations |
| `hey_jess_training.md` | Wake word training guide |

---

## Network Reference

| Service | Host | Port |
|---------|------|------|
| Brain Gateway | 10.0.0.186 | 8888 |
| Model Server (nginx) | 10.0.0.186 | 8080 |
| Nemotron (Saturn) | 10.0.0.58 | 8001 |
| Helios (Expert) | 10.0.0.195 | 8080 |
| Whisper STT (Uranus) | 10.0.0.173 | 8003 |
| Jessica TTS (Uranus) | 10.0.0.173 | 8002 |
| Home Assistant | 10.0.0.106 | 8123 |

---

## LED Color Reference

| Color | State |
|-------|-------|
| Off | Idle, waiting for wake word |
| Cyan | Wake word detected, starting voice assistant |
| Blue (pulsing) | Listening for command |
| Yellow | Processing (STT/Brain Gateway) |
| Green | Response playing on Google speaker |
| Red | Error occurred |

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│                      ATOM Echo (per room)                    │
│  - Wake word detection ("Hey Jess")                         │
│  - Microphone for voice capture                             │
│  - LED feedback only (no TTS playback)                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     Home Assistant                           │
│  - Routes audio to Whisper STT                              │
│  - Sends text to Brain Gateway                              │
│  - Routes TTS response to Google speaker                    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 Google Cast Speaker (per room)               │
│  - High-quality audio playback                              │
│  - Jessica TTS voice output                                 │
└─────────────────────────────────────────────────────────────┘
```
