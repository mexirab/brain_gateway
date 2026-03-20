# Remote Development Workflow

Work on Brain Gateway from anywhere with persistent sessions that survive wifi drops.

## How It Works

- **mosh** — UDP-based SSH replacement that handles roaming, IP changes, and intermittent connectivity
- **tmux** — terminal multiplexer that keeps sessions alive on Jupiter regardless of client connection
- **dev-session.sh** — creates a 3-window tmux session (Claude Code, docker logs, shell) with auto git-pull

## First-Time Jupiter Setup

SSH into Jupiter and run the setup script once:

```bash
ssh labadmin@100.102.29.14
cd /opt/jupiter/gateway_mvp
git pull
bash scripts/setup-jupiter-claude.sh
```

This installs git hooks, ruff, and creates `.claude/settings.local.json` with pre-approved permissions.

## Mac Setup

Install mosh if you haven't:

```bash
brew install mosh
```

Add to `~/.zshrc`:

```bash
jdev() {
    mosh labadmin@100.102.29.14 -- tmux attach -t brain-dev 2>/dev/null \
        || mosh labadmin@100.102.29.14 -- bash /opt/jupiter/gateway_mvp/scripts/dev-session.sh brain
}

jcon() {
    mosh labadmin@100.102.29.14 -- tmux attach -t conjure-dev 2>/dev/null \
        || mosh labadmin@100.102.29.14 -- bash /opt/jupiter/conjure/scripts/dev-session.sh conjure
}
```

Then `source ~/.zshrc`.

## Daily Workflow

1. Open terminal, type `jdev`
2. Work in the tmux session — Claude Code is in window 1
3. If wifi drops or you close your laptop, the session persists on Jupiter
4. Come back, type `jdev` again — picks up exactly where you left off
5. When you push to main from either machine, GitHub Actions auto-deploys

## tmux Cheat Sheet

| Keys | Action |
|------|--------|
| `Ctrl-b 1` | Switch to Claude Code window |
| `Ctrl-b 2` | Switch to docker logs window |
| `Ctrl-b 3` | Switch to shell window |
| `Ctrl-b d` | Detach from session (keeps it running) |
| `Ctrl-b [` | Enter scroll/copy mode (q to exit) |

## Git Sync Model

- **Mac → Jupiter:** Push to main, Jupiter picks up changes via `git pull` (auto on session attach, or CI auto-deploy)
- **Jupiter → Mac:** Push to main from Jupiter, `git pull` on Mac
- **Rule:** Don't work on the same branch from both machines simultaneously

## When to Use Which

| Situation | Use |
|-----------|-----|
| Home, stable wifi | Desktop app (Mac) is fine |
| Coffee shop, spotty wifi | `jdev` (terminal on Jupiter) |
| Need to leave mid-session | `jdev` — session survives |
| Quick code check | Desktop app (Mac) |

## Troubleshooting

**mosh can't connect:**
```bash
# Check if mosh-server is installed on Jupiter
ssh labadmin@100.102.29.14 "which mosh-server"
# Check UFW allows mosh ports
ssh labadmin@100.102.29.14 "sudo ufw status | grep 60000"
```

**tmux session lost:**
```bash
# List active sessions
ssh labadmin@100.102.29.14 "tmux ls"
# Create a fresh one
jdev  # will auto-create if none exists
```

**Claude Code permission prompts on Jupiter:**
```bash
# Re-run setup to regenerate settings.local.json
ssh labadmin@100.102.29.14
cd /opt/jupiter/gateway_mvp
rm .claude/settings.local.json
bash scripts/setup-jupiter-claude.sh
```
