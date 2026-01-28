# Training the "Hey Jess" Wake Word

This guide covers training a custom microWakeWord model for the ATOM Echo.

---

## Quick Start Checklist

**Tomorrow's session - just follow these steps:**

- [ ] Open Colab: https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
- [ ] Set `target_word = "hey jess"`
- [ ] Run pronunciation cell, listen to output, adjust spelling if needed
- [ ] Run all cells (~1 hour training)
- [ ] Download `hey_jess.tflite`
- [ ] Copy to ESPHome config directory (`/config/esphome/` on HA)

**After training:**
- [ ] Flash ATOM Echo with ESPHome (https://esphome.io/projects/)
- [ ] Upload `atom_echo.yaml` config
- [ ] Test wake word, tune `probability_cutoff` if needed

---

## Option 1: Google Colab (Recommended - Free, No GPU Required)

### Steps

1. **Open the Colab Notebook**
   ```
   https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
   ```

2. **Configure the Wake Word**
   - Find the cell with `target_word = ""`
   - Set it to: `target_word = "hey jess"`

3. **Run Pronunciation Verification**
   - Run the pronunciation cell (takes ~30 seconds)
   - Listen to the generated audio samples
   - If "jess" sounds wrong, try alternatives:
     - `"hey jess"` (default)
     - `"hey jess-ih-kuh"` (if it says "guess")
     - `"hey jeh-ss"` (alternative phonetic)

4. **Run Training**
   - Run all remaining cells
   - Training takes approximately 1 hour on Colab's free tier
   - The notebook will show training progress and accuracy metrics

5. **Download the Model**
   - After training completes, download `hey_jess.tflite`
   - Save it to your ESPHome config directory

### Expected Output
- Model file: `hey_jess.tflite` (~200KB)
- Target accuracy: 85-95% detection rate
- False positive rate: <5%

---

## Option 2: Local Docker with GPU (Faster)

For faster training or iterating on the model:

### Prerequisites
- NVIDIA GPU with CUDA support
- Docker with nvidia-container-toolkit

### Steps

```bash
# Pull the microWakeWord Docker image
docker pull ghcr.io/tatertotterson/microwakeword:latest

# Create a directory for output
mkdir -p ~/microwakeword_output

# Run the training environment
docker run --rm -it \
  --gpus all \
  -p 8888:8888 \
  -v ~/microwakeword_output:/data \
  ghcr.io/tatertotterson/microwakeword:latest
```

1. Open `http://localhost:8888` in your browser
2. Navigate to the training notebook
3. Modify the wake word to `"hey jess"`
4. Run all cells
5. Download the model from `/data/hey_jess.tflite`

### GPU Training Time
- RTX 3090/4090: ~10-15 minutes
- RTX 3060: ~20-30 minutes
- T4 (Colab-equivalent): ~45-60 minutes

---

## Tuning the Probability Cutoff

The `probability_cutoff` parameter in `atom_echo.yaml` controls sensitivity:

| Cutoff | Behavior |
|--------|----------|
| 0.3 | Very sensitive - catches most wake words, more false positives |
| 0.5 | Balanced (default) - good starting point |
| 0.7 | Conservative - fewer false positives, may miss quiet/mumbled words |
| 0.9 | Very strict - only clear, loud wake words trigger |

### Tuning Process

1. **Start with 0.5** (default)
   ```yaml
   micro_wake_word:
     models:
       - model: hey_jess.tflite
         probability_cutoff: 0.5
   ```

2. **Test for False Positives**
   - Leave the device running during normal conversation
   - Note if it triggers on similar-sounding phrases
   - Common false positives: "hey guess", "hey yes", "hey chess"

3. **Test Detection Rate**
   - Say "Hey Jess" at various volumes and distances
   - Note if it misses your natural speaking voice

4. **Adjust Based on Results**
   - Too many false positives → increase to 0.6 or 0.7
   - Missing your voice → decrease to 0.4 or 0.3

### Sliding Window Size

The `sliding_window_size` parameter affects responsiveness:

```yaml
micro_wake_word:
  models:
    - model: hey_jess.tflite
      probability_cutoff: 0.5
      sliding_window_size: 5  # Default: requires 5 consecutive positive frames
```

- Lower values (3-4): More responsive but more false positives
- Higher values (6-8): More stable but slightly slower to trigger

---

## Iteration Strategy

Expect 3-5 iterations to get good accuracy. After each iteration:

1. **Upload the new model** to ESPHome config directory
2. **Recompile and upload** the ESPHome firmware
3. **Test for 30 minutes** in your environment
4. **Log false positives** and missed detections
5. **Retrain** if needed with adjusted pronunciation or settings

### Common Issues

| Issue | Solution |
|-------|----------|
| Never triggers | Check pronunciation, lower cutoff, verify mic is working |
| Triggers on "Hey [name]" | Add negative examples in training, increase cutoff |
| Works close but not far | Lower cutoff, check mic placement/direction |
| Triggers on background noise | Increase cutoff, increase sliding_window_size |

---

## File Locations

After training, place the model file here:

```
ESPHome Config Directory/
├── atom_echo.yaml          # Device configuration
├── hey_jess.tflite         # Trained wake word model
└── secrets.yaml            # WiFi/API credentials
```

The ESPHome config directory location depends on your setup:
- **Home Assistant Add-on**: `/config/esphome/`
- **Standalone ESPHome**: `~/.esphome/` or custom location

---

## Verification

After uploading the model and configuration:

1. **Check ESPHome Logs**
   ```
   INFO microWakeWord: Loaded model: hey_jess.tflite
   ```

2. **Say "Hey Jess"**
   - LED should turn cyan/green
   - Log should show: `Wake word detected: Hey Jess`

3. **Device Should Start Listening**
   - LED pulses blue while listening
   - Audio streams to Home Assistant
