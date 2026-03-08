# Helios Manual Start Setup

## Overview

Helios runs the Qwen3-32B expert model (Qwen3-32B-Q5_K_M.gguf) via llama.cpp on port 8080,
fitting entirely in VRAM (~24GB on RTX 5090). This consumes ~150W continuously.
For your ADHD support use case, this model is rarely needed - Nemotron-8B handles 95%+ of tasks.

## One-Time Setup (Run on Helios)

### Step 1: Disable Auto-Start

SSH into Helios and disable auto-start:

```bash
ssh nadim@10.0.0.195

# Stop the currently running service
sudo systemctl stop llama-server

# Disable auto-start on boot
sudo systemctl disable llama-server

# Verify it's disabled
sudo systemctl is-enabled llama-server  # Should show "disabled"
```

### Step 2: Configure Passwordless Sudo (Optional but Recommended)

To allow the start/stop scripts to work from Jupiter without password prompts:

```bash
# On Helios, create a sudoers rule
sudo visudo -f /etc/sudoers.d/llama-server

# Add this line:
nadim ALL=(ALL) NOPASSWD: /usr/bin/systemctl start llama-server, /usr/bin/systemctl stop llama-server, /usr/bin/systemctl status llama-server

# Save and exit
```

This allows only the specific systemctl commands without a password.

If you skip this step, you'll need to SSH to Helios manually to start/stop the service.

## Daily Usage

From Jupiter, use these scripts:

```bash
# Check if Helios is running
./scripts/helios-status.sh

# Start when you want deep conversations
./scripts/start-helios.sh

# Stop when done (saves power)
./scripts/stop-helios.sh
```

## Power Savings

| State | Power Draw |
|-------|------------|
| Running | ~150W |
| Stopped | ~15W (idle GPU) |

**Monthly savings:** ~$10-15 at typical electricity rates

## When to Use Helios

The orchestrator's `ask_expert` tool will automatically try to use Helios.
If Helios is off, the tool will gracefully fail and Nemotron will handle it.

Good use cases for the Qwen3-32B model:
- Complex technical deep dives
- Nuanced emotional support conversations
- Creative writing or brainstorming
- Research synthesis

Your daily ADHD support (reminders, motivation, spirals) works great with Nemotron-8B.
