# iPhone Voice Input via Tailscale HTTPS

iOS Safari requires HTTPS for microphone access. This guide enables voice input on your iPhone by accessing Open WebUI through Tailscale's automatic HTTPS.

## Your Tailscale Details

| Item | Value |
|------|-------|
| Machine Name | `voyager-970` |
| Full DNS Name | `voyager-970.tail74fc4a.ts.net` |
| HTTPS URL | `https://voyager-970.tail74fc4a.ts.net` |
| iPhone Device | `iphone172` (already connected) |

## Quick Setup (Run on Voyager)

### Step 1: Enable Tailscale Serve

Run these commands on Voyager with sudo:

```bash
# Allow your user to run tailscale commands without sudo (one-time)
sudo tailscale set --operator=$USER

# Set up HTTPS proxy to Open WebUI
tailscale serve --bg http://localhost:80
```

Or if you prefer to keep using sudo:

```bash
sudo tailscale serve --bg http://localhost:80
```

### Step 2: Verify Setup

```bash
tailscale serve status
```

You should see output like:
```
https://voyager-970.tail74fc4a.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:80
```

### Step 3: Access from iPhone

1. Open Safari on your iPhone
2. Navigate to: **https://voyager-970.tail74fc4a.ts.net**
3. Log in to Open WebUI
4. Tap the microphone button - you should get the permission prompt

## Configuration

### Enable Audio in Open WebUI

1. Click the gear icon (Settings)
2. Go to **Audio**
3. Enable **Speech-to-Text (STT)**
4. Enable **Text-to-Speech (TTS)**
5. Set STT engine to Whisper (if available)

### iOS Microphone Permissions

If you don't see the permission prompt:
1. Go to iPhone **Settings** > **Safari** > **Settings for Websites**
2. Find **Microphone**
3. Ensure it's set to **Ask** or **Allow**

## Verification Checklist

- [ ] `tailscale status` shows voyager-970 connected
- [ ] `tailscale serve status` shows the HTTPS proxy configured
- [ ] iPhone Tailscale app shows connected (green dot)
- [ ] `https://voyager-970.tail74fc4a.ts.net` loads Open WebUI
- [ ] Microphone permission prompt appears (not "denied")
- [ ] Voice input transcribes correctly
- [ ] TTS responses play with Jessica voice

## Troubleshooting

### "Site can't be reached"
- Verify iPhone Tailscale app is connected
- Check `tailscale status` shows both devices
- Try disconnecting and reconnecting Tailscale on iPhone

### Certificate error
- Tailscale handles certificates automatically
- Wait a few minutes for DNS propagation
- Try force-refreshing the page

### Microphone still denied
- Clear Safari data: Settings > Safari > Clear History and Website Data
- Or just clear data for this site in Safari settings
- Try Safari private browsing window
- Check iOS Settings > Safari > Privacy for microphone permissions

### No audio output
- Check iPhone volume and silent mode
- Ensure TTS is enabled in Open WebUI settings
- Test with a simple chat message first

## How It Works

```
iPhone Safari (HTTPS required for mic)
         │
         ▼
https://voyager-970.tail74fc4a.ts.net:443
         │
         ▼
   Tailscale Serve (HTTPS termination)
         │
         ▼
   http://localhost:80
         │
         ▼
   Open WebUI Container
```

Tailscale Serve acts as a reverse proxy, providing:
- Automatic Let's Encrypt certificates for `*.ts.net` domains
- HTTPS termination on port 443
- Proxying to the local HTTP service

## Stopping the HTTPS Proxy

If you need to disable the HTTPS proxy:

```bash
tailscale serve --bg --remove /
# or
tailscale serve reset
```

## Alternative: Direct Tailscale HTTPS (Without Serve)

If Tailscale Serve doesn't work, you can configure Caddy or nginx to use Tailscale certificates:

```bash
# Provision certificates (requires sudo)
sudo tailscale cert voyager-970.tail74fc4a.ts.net

# Certificates will be at:
# /var/lib/tailscale/certs/voyager-970.tail74fc4a.ts.net.crt
# /var/lib/tailscale/certs/voyager-970.tail74fc4a.ts.net.key
```

Then configure your reverse proxy to use these certificates.
