# Off-box dead-man's switch (Saturn watches Jupiter)

Jupiter runs Prometheus and Alertmanager next to everything they watch. If Jupiter goes down as a whole, the watcher goes with it and nothing pages. On 2026-10-26 dockerd crashed and every container, monitoring included, stayed down for 2.5 h without a single page.

This folder holds a small service that runs on **Saturn** and pages you over Pushover when Jupiter's monitoring goes silent:

```
Jupiter                                              Saturn
Prometheus: alert "Watchdog" (fires while Jupiter can page you)
   -> Alertmanager re-sends it about every 2 min  --POST /heartbeat (bearer)-->  jupiter-deadman.service
                                                                              | no heartbeat for 10 min?
Prometheus scrapes Saturn:9466/metrics  <-----------------------------------  | -> Pushover page (priority 1)
  (DeadmanSwitchBroken if the switch dies)                                    |    + what Saturn can still reach
```

A heartbeat proves the whole alert path works: Jupiter is up, Prometheus is ingesting data and evaluating rules, Alertmanager is delivering notifications, and Jupiter's own Pushover deliveries aren't failing. Each minute Saturn also probes Jupiter directly (orchestrator `/health`, Home Assistant, the SSH port), so the page says *what* is down:

| Saturn sees | The page says |
|---|---|
| nothing answers | the box is frozen, powered off, or off the network |
| SSH answers, orchestrator + HA don't | Docker or the containers are likely down (the 2026-10-26 case) |
| orchestrator + HA answer | only the monitoring stack (Prometheus/Alertmanager), Jupiter's Pushover delivery, or the heartbeat route/token is broken |

The probes never page on their own: while heartbeats arrive, Jupiter's own alerts (OrchestratorDown, ...) handle paging, and a second page from here would duplicate them.

Files:

| File | Goes to (on Saturn) | What |
|---|---|---|
| `jupiter_deadman.py` | `/opt/jupiter-deadman/jupiter_deadman.py` | The service. Python standard library only, no venv, no Docker. |
| `jupiter-deadman.service` | `/etc/systemd/system/jupiter-deadman.service` | systemd unit: restarts on failure, throwaway user, sandboxed |
| `jupiter-deadman.env.example` | `/etc/jupiter-deadman.env` (mode 600) | Settings and secrets. The real file never goes in git. |

Design notes and the runbook live in `monitoring/README.md` → Dead-man's switch.

---

## Install, step by step

Install Saturn first and Jupiter second. Saturn starts in **dry-run**, so it only logs what it would send. That way the half-installed state never pages you.

Commands marked **[Jupiter]** run in `~/gateway_nerves` on Jupiter; **[Saturn]** ones run on Saturn (`ssh labadmin@10.0.0.58`).

### 1. Make the shared secret [Jupiter]

```bash
openssl rand -hex 24
```

*What it does:* prints 48 random hex characters. Jupiter's Alertmanager sends this token with every heartbeat, and Saturn rejects any heartbeat without it, so another device on the LAN can't fake "Jupiter is fine". Keep it in your password manager for now. Don't add it to Jupiter's `.env` yet: that happens in step 7.

### 2. (Recommended) a separate Pushover application

At <https://pushover.net/apps/build>, create an app called e.g. "Jupiter dead-man" and copy its **API token**. Your **user key** is the same one Jupiter uses.

*Why separate:* the page then arrives under its own name, so it's obvious it came from Saturn. You can also revoke that token without touching Jupiter's alerts. Reusing Jupiter's app token also works.

### 3. Copy the files to Saturn [Jupiter]

```bash
ssh labadmin@10.0.0.58 mkdir -p jupiter-deadman
scp monitoring/deadman/jupiter_deadman.py monitoring/deadman/jupiter-deadman.service monitoring/deadman/jupiter-deadman.env.example labadmin@10.0.0.58:jupiter-deadman/
```

*What it does:* puts the three files in `~/jupiter-deadman/` on Saturn. Nothing is installed or running yet.

### 4. Try it by hand first [Saturn]

```bash
python3 ~/jupiter-deadman/jupiter_deadman.py probe
```

*What it does:* runs one round of the checks Saturn will make every minute and prints them. It sends nothing. Expected output while Jupiter is healthy:

```
orchestrator     up    http 200
home_assistant   up    http 200
jupiter_ssh      up    port open
All probes answered.
```

`DOWN refused` means the port is closed. `DOWN timeout` means no answer at all. `DOWN not HTTP` means something answered, but not a web server. `DOWN SSLError errno N` means TLS failed on an `https://` probe (wrong scheme or port, or a bad certificate).

Also check that Saturn can look up Pushover's address. It will need that during an outage, so it must not depend on anything running on Jupiter:

```bash
getent hosts api.pushover.net     # prints an address; nothing printed = DNS is broken
resolvectl dns                    # which DNS servers Saturn uses (should be the router, not Jupiter)
```

### 5. Install the script and the settings file [Saturn]

```bash
sudo install -D -m 0644 ~/jupiter-deadman/jupiter_deadman.py /opt/jupiter-deadman/jupiter_deadman.py
sudo install -m 0600 -o root -g root ~/jupiter-deadman/jupiter-deadman.env.example /etc/jupiter-deadman.env
sudo nano /etc/jupiter-deadman.env
```

*What it does:* `install` copies a file and sets its permissions in one step (`-D` creates `/opt/jupiter-deadman/`). The settings file is `0600 root`: only root can read it, and systemd reads it as root before starting the service as an unprivileged user. In the editor, fill in:

- `DEADMAN_WEBHOOK_TOKEN=` the token from step 1
- `PUSHOVER_USER_KEY=` and `PUSHOVER_APP_TOKEN=` (step 2)
- leave `DEADMAN_ALLOW_FROM=10.0.0.248` (only Jupiter, plus Saturn itself, may connect at all) and `DEADMAN_DRY_RUN=true`

Check that Saturn can page you. This sends one low-priority test notice, even in dry-run:

```bash
sudo bash -c 'set -a; . /etc/jupiter-deadman.env; exec python3 -I /opt/jupiter-deadman/jupiter_deadman.py test-notify'
```

*Why `bash -c` with `set -a`:* it loads the settings file into the environment of that one command, so the secrets never show up on a command line (where `ps` could see them). You should get "Jupiter dead-man: test notice" on your phone.

### 6. Open the port and start the service [Saturn]

```bash
sudo ufw status
```

If it says `Status: active`, allow Jupiter (and only Jupiter) to reach port 9466:

```bash
sudo ufw allow from 10.0.0.248 to any port 9466 proto tcp comment 'jupiter-deadman'
```

If it says `inactive`, leave it that way for now. The service drops every connection that isn't from Jupiter or Saturn itself the moment it is accepted, before reading a byte, so other devices on the LAN can't send heartbeats, read `/metrics`, or tie the service up.

Turning ufw on is a project of its own: it blocks everything not explicitly allowed. You'd first need rules for SSH (`sudo ufw allow OpenSSH`, or you lock yourself out) and for what Jupiter reads from Saturn (node-exporter `:9100`, GPU exporter `:9400`, netdata `:19999`, the nightly backups over SSH). Then start the service:

```bash
sudo install -m 0644 ~/jupiter-deadman/jupiter-deadman.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jupiter-deadman
systemctl status jupiter-deadman
journalctl -u jupiter-deadman -f      # Ctrl-C to stop watching
```

*What it does:* `daemon-reload` makes systemd read the new unit, and `enable --now` starts it and also starts it at every boot. The journal should show `listening on 0.0.0.0:9466 (accepting 10.0.0.248 + loopback); page after 600 s without a heartbeat; ...; state: /var/lib/jupiter-deadman/state.json; DRY RUN (nothing is sent)`. The state file keeps the last heartbeat time, so a restart doesn't reset the 10-minute clock.

`journalctl -u jupiter-deadman -p warning` shows only the warnings and errors.

Jupiter isn't sending heartbeats yet, so after about 10 minutes the journal shows `DRY RUN, not sent: priority 1 'Jupiter dead-man: no heartbeat for 10 min': …`, followed by the full page text. That is the alarm working, minus the send.

Curious how tightly systemd locks it down? `systemd-analyze security jupiter-deadman` scores the sandbox; lower is tighter.

### 7. Point Jupiter at Saturn [Jupiter]

Add the same token to `~/gateway_nerves/.env`:

```
DEADMAN_WEBHOOK_TOKEN=<the token from step 1>
```

Then render and reload the monitoring config:

```bash
bash scripts/generate-configs.sh      # expect: "OK: dead-man's switch token -> deadman_webhook_token (heartbeats to 10.0.0.58:9466)"
bash scripts/reload-monitoring.sh
```

A few minutes later, check that heartbeats arrive:

```bash
curl -s http://10.0.0.58:9466/metrics | grep -E 'heartbeats_total\{result="ok"\}|last_heartbeat'
curl -s -G localhost:9090/api/v1/query --data-urlencode 'query=up{job="jupiter-deadman"}'
```

These `curl` commands work from Jupiter (its address is allowed); from any other machine the connection just closes. The `ok` count should go up about every 2 minutes, and `up` should be `1`. Saturn's journal says `first heartbeat received from Alertmanager`, followed by a dry-run "heartbeat is back" line if step 6's dry-run page had already fired.

If the count stays at 0, Saturn's journal names the reason: `rejected heartbeat (unauthorized)` means the tokens differ, and `(forbidden) from <address>` means the connection came from an address that isn't in `DEADMAN_ALLOW_FROM`.

### 8. Arm it [Saturn]

```bash
sudo sed -i 's/^DEADMAN_DRY_RUN=true/DEADMAN_DRY_RUN=false/' /etc/jupiter-deadman.env
sudo systemctl restart jupiter-deadman
journalctl -u jupiter-deadman -n 5     # the "DRY RUN" note is gone from the listening line
```

If you skip this, Jupiter sends a quiet `DeadmanSwitchBroken` notice after 30 minutes.

---

## Test plan (it really pages)

**A. Jupiter's monitoring goes silent (about 8-12 min).** [Jupiter]

```bash
docker stop alertmanager
```

About 8-12 minutes later your phone gets **"Jupiter dead-man: no heartbeat for 10 min"**. The message says Jupiter's services still answer, so it's the monitoring stack. Then:

```bash
docker start alertmanager
```

Within about 2 minutes you get a low-priority **"Jupiter dead-man: heartbeat is back"**. While Alertmanager is stopped, Jupiter's own alerts can't reach you, so keep the test short.

**B. The watcher dies (about 30 min).** [Saturn] `sudo systemctl stop jupiter-deadman`. After about 30 minutes, Jupiter sends a quiet **"DeadmanSwitchBroken"** notice. Then run `sudo systemctl start jupiter-deadman`.

**C. Dry-run rehearsal (no phone involved).** Any time, on Saturn: set `DEADMAN_DRY_RUN=true`, restart, and repeat test A. The journal shows the exact page text with `DRY RUN, not sent`.

---

## Day to day

- **Planned Jupiter downtime** (reboot, maintenance longer than ~10 min): run `sudo systemctl stop jupiter-deadman` on Saturn first and `start` it once Jupiter is back. After a start it waits 3 minutes for a fresh heartbeat before paging, so the old saved time doesn't page you. Never silence the `Watchdog` alert in Alertmanager: that stops the heartbeats and pages you too, by design.
- **Updating the script:** repeat step 3, then `sudo install -m 0644 ~/jupiter-deadman/jupiter_deadman.py /opt/jupiter-deadman/` and `sudo systemctl restart jupiter-deadman`. CI never deploys to Saturn.
- **Changing a setting:** `sudo nano /etc/jupiter-deadman.env`, then `sudo systemctl restart jupiter-deadman`.
- **Uninstall:** `sudo systemctl disable --now jupiter-deadman`, then `sudo rm /etc/systemd/system/jupiter-deadman.service /etc/jupiter-deadman.env`, `sudo rm -r /opt/jupiter-deadman` and `sudo rm -r /var/lib/private/jupiter-deadman` (the saved state). After that, remove `DEADMAN_WEBHOOK_TOKEN` from Jupiter's `.env` and re-run step 7's two scripts; that also empties Jupiter's copy of the token file.

## Settings (`/etc/jupiter-deadman.env`)

| Variable | Default | What |
|---|---|---|
| `DEADMAN_WEBHOOK_TOKEN` | required, ≥ 24 chars | Same value as in Jupiter's `.env` |
| `PUSHOVER_USER_KEY` / `PUSHOVER_APP_TOKEN` | required unless dry-run | Who gets paged, and through which Pushover app |
| `DEADMAN_ALLOW_FROM` | `DEADMAN_TARGET_HOST` | Comma list of IPs allowed to connect at all (loopback always is). Everything else is dropped on accept |
| `DEADMAN_DRY_RUN` | `false` | `true` = log pages instead of sending them (`test-notify` still sends) |
| `DEADMAN_LISTEN_ADDR` / `DEADMAN_LISTEN_PORT` | `0.0.0.0` / `9466` | Where it listens |
| `DEADMAN_HEARTBEAT_MAX_AGE_SECONDS` | `600` (min 300) | Silence that triggers a page |
| `DEADMAN_CHECK_INTERVAL_SECONDS` | `60` (max 120) | How often it checks and probes |
| `DEADMAN_REPEAT_HOURS` | `4` | Re-page interval while Jupiter stays silent |
| `DEADMAN_TARGET_HOST` | `10.0.0.248` | Jupiter's address for the default probes |
| `DEADMAN_PROBES` | orchestrator `:8888/health`, HA `:8123/manifest.json`, `tcp :22` | `name=http://...` / `name=tcp://host:port`, comma-separated |
| `DEADMAN_PROBE_TIMEOUT_SECONDS` | `5` | Total time allowed per probe |
| `DEADMAN_STATE_DIR` | systemd's `$STATE_DIRECTORY` | Where `state.json` (last heartbeat, alert state) is kept; unset when run by hand = not saved |

Endpoints: `POST /heartbeat` (bearer, from Alertmanager), `GET /metrics` (Prometheus), `GET /healthz`.
