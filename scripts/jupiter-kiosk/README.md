# Closet monitors kiosk (Jupiter)

The two portrait ViewSonic monitors in the server closet show Grafana's **Jess Glance** dashboard
(left) and the **Jess dashboard** `/dashboard` page (right, without the sidebar). Both are live
pages, and nobody has to touch them: the kiosk logs itself in, retries when a server is down or
still starting, waits while the monitors are off, and restarts a dead browser.

```
jupiter-kiosk.service (User=kiosk, tty1, PAM service jupiter-kiosk → logind seat session)
 └─ start-x.sh ── xinit ──┬─ Xorg :0  (rootless, pinned to the bus-0c RTX 5080, no input devices)
                          └─ kiosk.py ──┬─ Chrome (profile "left")  ⇄ DevTools pipe
                                        └─ Chrome (profile "right") ⇄ DevTools pipe
```

**Which GPU:** the monitors hang off the RTX 5080 on PCI bus **0c** (the one with two HDMI ports).
The voice (`voxcpm-tts`) runs on the bus-**0b** card. Three things keep the kiosk off it:
- the X config pins X to bus 0c (`BusID` + `ProbeAllGpus "false"`);
- the unit hides the voice card's device files (`InaccessiblePaths=/dev/nvidia0 /dev/dri/card0
  /dev/dri/renderD128`);
- `kiosk.py --preflight` (ExecStartPre) refuses to start if the display card is the one
  `/etc/voxcpm-tts.env` pins, or if those masks hide the display card instead.

Measured 2026-11-24: X ~66 MiB plus Chrome ~105 MiB on bus 0c, and no memory or context on the voice
card.

## Files

| Repo file | Installed as | What |
|---|---|---|
| `kiosk.py` | `/usr/local/lib/jupiter-kiosk/kiosk.py` | The driver. Its docstring is the spec. |
| `start-x.sh` | `/usr/local/lib/jupiter-kiosk/start-x.sh` | Makes the X auth cookie, then runs `xinit` |
| `jupiter-kiosk.xorg.conf` | `/etc/X11/jupiter-kiosk.conf` | X on the display card only, no input devices |
| `jupiter-kiosk.service` | `/etc/systemd/system/jupiter-kiosk.service` | The unit (exec-time hardening) |
| `jupiter-kiosk.pam` | `/etc/pam.d/jupiter-kiosk` | Minimal PAM stack for the seat session |
| `jupiter-kiosk.polkit.rules` | `/etc/polkit-1/rules.d/10-jupiter-kiosk.rules` | Kiosk user may not power off, reboot or mount anything |
| `kiosk-user-slice.conf` | `/etc/systemd/system/user-<kiosk uid>.slice.d/50-jupiter-kiosk.conf` | Memory/task/CPU limits + network allow-list |
| `kiosk-user-manager.conf` | `/etc/systemd/system/user@<kiosk uid>.service.d/50-jupiter-kiosk.conf` | Same hardening for the kiosk user's systemd manager |
| `kiosk.example.json` | `/etc/jupiter-kiosk/kiosk.json` (root:kiosk 0640) | Screens, URLs, rotation. No secrets |
| `make_logins.py` | writes `/etc/jupiter-kiosk/secrets.json` (root:kiosk 0640) | Grafana viewer login + dashboard cookie |

`make_logins.py` isn't called `make_secrets.py` because the repo `.gitignore` drops every `*secret*`
file.

## Install (one-time, as labadmin on Jupiter)

```bash
sudo apt-get install -y xinit xauth
sudo apt-get remove -y xserver-xorg-legacy   # a recommend of xinit: setuid X wrapper, not needed for rootless X
sudo useradd --system --create-home --home-dir /var/lib/jupiter-kiosk --shell /usr/sbin/nologin --comment "Closet monitors kiosk" kiosk
cd /home/labadmin/gateway_nerves/scripts/jupiter-kiosk
sudo install -d -m 755 /usr/local/lib/jupiter-kiosk
sudo install -m 755 kiosk.py start-x.sh /usr/local/lib/jupiter-kiosk/
sudo install -m 644 jupiter-kiosk.xorg.conf /etc/X11/jupiter-kiosk.conf
sudo install -m 644 jupiter-kiosk.pam /etc/pam.d/jupiter-kiosk
sudo install -m 644 jupiter-kiosk.polkit.rules /etc/polkit-1/rules.d/10-jupiter-kiosk.rules
sudo install -d -m 755 /etc/systemd/system/user-$(id -u kiosk).slice.d
sed "s/@TAILSCALE_IP@/$(tailscale ip -4)/" kiosk-user-slice.conf \
  | sudo install -m 644 /dev/stdin /etc/systemd/system/user-$(id -u kiosk).slice.d/50-jupiter-kiosk.conf
sudo install -d -m 755 /etc/systemd/system/user@$(id -u kiosk).service.d
sudo install -m 644 kiosk-user-manager.conf /etc/systemd/system/user@$(id -u kiosk).service.d/50-jupiter-kiosk.conf
sudo install -d -m 750 -o root -g kiosk /etc/jupiter-kiosk
sudo install -m 640 -o root -g kiosk kiosk.example.json /etc/jupiter-kiosk/kiosk.json
sudoedit /etc/jupiter-kiosk/kiosk.json      # host_map IP = `tailscale ip -4`; outputs/rotation (below)
sudo python3 -I make_logins.py              # checks the dashboard cookie, makes Grafana user closet-kiosk
sudo -u kiosk /usr/local/lib/jupiter-kiosk/kiosk.py --preflight   # warns about the voice card: masks apply inside the unit only
sudo install -m 644 jupiter-kiosk.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jupiter-kiosk
journalctl -t jupiter-kiosk -b | grep -E 'preflight|browser up'   # inside the unit: no voice-card warning
```

Starting it takes tty1 away from the console login (`Conflicts=getty@tty1`). X ignores keyboards, so
Ctrl+Alt+F2 does nothing while it runs. Use SSH:
`sudo systemctl stop jupiter-kiosk && sudo systemctl start getty@tty1` brings the text console back.

`xinit` pulls in about 25 packages through recommends: `xserver-xorg`, the input drivers, and
userspace X video drivers for AMD/Intel/nouveau that never load, since the config names `nvidia`.
None of them is a kernel module.

## Config (`/etc/jupiter-kiosk/kiosk.json`)

Unknown keys are refused, so a typo stops the kiosk with a clear log line instead of being ignored.

- `chrome`: `/opt/google/chrome/chrome`, the binary rather than the `google-chrome-stable` wrapper
  (whose helper processes would inherit the control pipe). When apt replaces it, kiosk.py restarts
  the browsers on the new version.
- `timezone`: IANA name (`America/Chicago`), passed to Chrome as `TZ`. Jupiter itself runs on UTC,
  and both pages format times in the browser's timezone.
- `host_map`: Jupiter's own resolver doesn't know `*.ts.net` (MagicDNS is off on the host), so
  Chrome maps the tailnet name to Jupiter's Tailscale IP (`tailscale ip -4`). Using the real https
  names means cookies, CSRF checks and `GF_SECURITY_COOKIE_SECURE` behave exactly as on the phone.
- `screens[]`, left to right:
  - `name`: lowercase; also the Chrome profile folder.
  - `output`: xrandr name under the NVIDIA X driver (`HDMI-0`, `HDMI-1`, …, not the kernel's `HDMI-A-2`).
    If missing or not connected, the screen takes the next free connected output.
  - `rotate`: `left` / `right` for a portrait monitor, `normal`, or `inverted`.
  - `url`, which must be https for any login.
  - `login`: `grafana` | `dashboard` | `none`.
  - `reload_minutes`: 0 = never. The dashboard cards don't poll, so the dashboard reloads every
    5 min; Grafana refreshes itself with `&refresh=30s`.
  - `hide_nav`: sets the `__Host-jess_display=kiosk` cookie. The dashboard then shows no sidebar and no
    bottom tab bar (`frontend/src/lib/display-mode.ts`).

Live (2026-11-24): `HDMI-0` = left monitor, rotate `left`; `HDMI-1` = right monitor, rotate `right`.
**Picture sideways or upside down:** swap that screen's `rotate` between `left` and `right`.
**Screens swapped:** swap the two `output` values. Then `sudo systemctl restart jupiter-kiosk`.

## Secrets

`make_logins.py` reads the repo `.env` and changes things in this order, so a failure leaves nothing
half-changed:
1. **Dashboard:** derives the session **cookie value**, `HMAC(DASHBOARD_TOKEN, label)`, rather than
   storing the password, and checks it against the running dashboard.
2. **Grafana:** creates (or re-enables and re-passwords) the user `closet-kiosk`: org 1 Viewer,
   never a Grafana admin, random password. It sends the admin password only after checking that
   docker-proxy owns 127.0.0.1:3000, and never across a redirect.
3. Writes `secrets.json`.

The cookie still opens the whole dashboard (chat, Home Assistant controls, settings), so the file is
root:kiosk 0640. In the browser it is a session cookie, set again at every start, and the Chrome
profiles live in the kiosk user's runtime dir (`/run/user/<uid>/jupiter-kiosk/profiles`, tmpfs,
cleared when the kiosk stops). No cookie, cache or crash dump reaches the disk. **Re-run it after changing `DASHBOARD_TOKEN`**, then restart the kiosk. Until you
do, the right screen sits on the login page, and the log says `dashboard asked for a login` every
5 min.

## Day to day

```bash
systemctl status jupiter-kiosk
journalctl -t jupiter-kiosk -f               # kiosk.py + X + preflight (they run in the PAM session scope)
sudo systemctl restart jupiter-kiosk
sudo pkill -USR1 -u kiosk -x python3         # snapshot each screen (kiosk.py is the kiosk user's only python3) →
sudo ls -l /run/user/$(id -u kiosk)/jupiter-kiosk/   #   left.png / right.png: personal data, delete after looking
nvidia-smi                                   # Xorg + chrome must show on GPU bus 0C only
systemd-cgls -u user-$(id -u kiosk).slice    # what the kiosk runs; `systemctl status user-$(id -u kiosk).slice` = memory
```

The X server's own log is `/var/lib/jupiter-kiosk/.local/share/xorg/Xorg.0.log`. A plain
`systemctl stop` leaves the unit "failed", because xinit exits 1 on SIGTERM.

**What the log says:**
- `page not loading (HTTP 502)`: the server behind Tailscale Serve is down or still starting, e.g.
  Grafana right after a reboot. It retries every 30 s and says `page loading again` when it's back.
- `no monitor connected … looking again every 30 s`: both monitors are off or unplugged. It starts
  by itself once one comes back.
- `monitors changed … restarting`: a monitor was plugged in or out. systemd restarts it with a
  fresh layout 30 s later.
- `browser restart (…)`: Chrome died, hung, crashed 3 times in 5 min, or was upgraded. It restarts
  with backoff (5 s up to 2 min).

**Chrome won't start** (`browser restart (Target.getTargets: connection closed)` every few seconds):
Chrome's stderr is dropped because it can carry page URLs. To see it:
`sudo systemctl edit jupiter-kiosk`, add `[Service]` / `Environment=KIOSK_DEBUG_CHROME=1`, restart,
and read the journal. **Then remove the drop-in, `sudo systemctl daemon-reload` and restart again**:
removing it alone leaves the running browsers logging.

## Security

- **The `kiosk` user:** no password, no shell, no sudo, no docker group. Everything (X, Chrome, the
  driver) runs as it. Its own systemd manager (`user@<uid>.service`), which Chrome talks to over D-Bus,
  gets the same no-new-privileges and hidden paths (`kiosk-user-manager.conf`). Without that, code
  running as kiosk could start a unit through it outside the kiosk unit's hardening.
- **polkit:** denies the user every action. As the active seat session it would otherwise be allowed
  to power off or reboot Jupiter and mount disks without a password.
- **No input devices:** X adds no keyboards or mice, and VT switching and Ctrl+Alt+Backspace are off.
  A keyboard plugged into Jupiter can't drive a browser that holds the dashboard session (DevTools,
  `file://` pages).
- **Nothing outside the unit can reach X**, and anything that could would see the screens. Three
  layers stop it:
  - a fresh MIT-MAGIC-COOKIE per start, passed to xauth on stdin;
  - `-nolisten tcp -nolisten local`: no TCP port and no abstract socket. An abstract socket would be
    reachable from host-networked containers such as Home Assistant;
  - the remaining socket lives in the unit's `PrivateTmp`.
- **Chrome:**
  - driven over `--remote-debugging-pipe`, so no DevTools port is opened;
  - its own user-namespace sandbox, plus `NoNewPrivileges`, seccomp and the unit's other exec-time
    hardening;
  - background networking, sync, component updates, extensions and permission prompts are off.
- **One profile per screen:** this is load-bearing. Cookies aren't port-scoped, so a shared profile
  would send the dashboard cookie to Grafana on :3000 and every other Tailscale Serve app on that
  hostname. Both browsers still run as the same OS user.
- **Grafana login:** runs in an isolated JavaScript world, so the page's own scripts can't hook it.
  It also checks in the page that it's still on Grafana's login page before sending the password.
- **Limits on the user slice:** 3 GiB memory, 1024 tasks, low CPU weight. The only network
  destination is Jupiter's Tailscale address, not even loopback: several loopback-only services on
  Jupiter have no auth (Alertmanager, Prometheus, Blackbox `/probe`, SearXNG, Loki). Chrome can't
  reach Google or the internet. Other ports on the Tailscale address (SSH, the orchestrator, HA) stay
  reachable, as they are from the LAN.
- **Crash reports:** Chrome still starts its crashpad handlers, but nothing can be uploaded (no
  network, consent off), and dumps land in the tmpfs profile.
- **Hidden folders:** the unit hides `/mnt`, `/home`, `/root`, `/srv` and a few `/opt` app folders.
  Some files there are world-readable (other apps' data), and a browser has no business reading
  them.
- **Logs:** screen names, states, HTTP status codes and error kinds only. SIGUSR1 snapshots show the
  screen as it is (calendar, tasks…), so they are mode 600 in the kiosk user's runtime dir, which is
  cleared on stop.

**Known limits:**
- **The NVIDIA sideband socket is reachable from the host network namespace.** Besides
  `/tmp/nvidia-xdriver-*` in PrivateTmp, the X driver always binds an abstract
  `@tmp/nvidia-xdriver-*` datagram socket, and `SidebandSocketPath` only renames it. Every process
  in the host network namespace can send to it, including the host-networked Home Assistant and
  Pi-hole containers. What it exposes is unauthenticated datagrams parsed by the closed-source
  driver ("NV-GLX sideband client notification"). No exploit is known; the risk is a parser bug or a
  flood. The fix is a private network namespace for X alone: split X into its own unit with
  `PrivateNetwork=yes`, and keep Chrome in a unit that shares only `/tmp` (`JoinsNamespaceOf=`).
- **X's startup probe still opens the voice card.** It asks logind for every DRM device, and logind
  opens `/dev/dri/card0` outside the unit's mount namespace and hands X the fd, so `InaccessiblePaths`
  can't stop it (`-isolateDevice` doesn't either). It holds no memory or context there. Putting the
  voice card on its own logind seat (`loginctl attach`) would end it, but that changes udev on the live
  voice card.
- **The dashboard cookie is a master credential:** the same value as every other device, it reaches
  every `/api/*` path through the dashboard proxy, and can't be revoked without changing
  `DASHBOARD_TOKEN` (which logs the phone out too). A separate read-only kiosk session in the
  frontend would fix that.
- **Chrome upgrades only by hand:** Google's apt source isn't in unattended-upgrades' allowed
  origins.

## Known limits (display)

- Jess Glance was laid out for landscape, so the bottom ~40% of the portrait Grafana screen is
  empty. A portrait dashboard (dashgen) would fill it.
- No night schedule: the screens stay on around the clock.
- The Tailscale IP sits in `host_map` and in the slice's `IPAddressAllow`. If Jupiter's tailnet
  address ever changes, edit both.
- **"No data" on the Grafana screen:** the request, latency, tool-call and LLM-error panels read 0
  when nobody has chatted or the orchestrator just restarted (`or on() vector(0)` in
  `jess_glance.py`). The temperature panels keep "No data" on purpose: there it means HA has no
  reading (the sensor is missing).

## Uninstall

```bash
sudo systemctl disable --now jupiter-kiosk && sudo systemctl start getty@tty1
sudo rm /etc/systemd/system/jupiter-kiosk.service /etc/X11/jupiter-kiosk.conf /etc/pam.d/jupiter-kiosk \
  /etc/polkit-1/rules.d/10-jupiter-kiosk.rules
sudo rm -r /etc/systemd/system/user-$(id -u kiosk).slice.d /etc/systemd/system/user@$(id -u kiosk).service.d \
  /usr/local/lib/jupiter-kiosk /etc/jupiter-kiosk
sudo systemctl daemon-reload
sudo userdel -r kiosk
# Grafana: delete user closet-kiosk (Administration → Users)
```
