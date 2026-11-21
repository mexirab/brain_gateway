"""Compare two VoxCPM2 TTS servers (runbook: docs/VOICE_AND_TTS.md → VoxCPM2 on Jupiter, step 2).

Generic text only: never put personal data in a test sentence. Stdlib only, so it runs in a
throwaway container from the orchestrator image, which is how it must run on Jupiter: the Jupiter
TTS admits Docker networks only, and a host-side request times out.

  docker run --rm --network bridge --add-host host.docker.internal:host-gateway -e TTS_VOICE=jessica \\
    -e TTS_API_KEY="$(sudo cat /etc/voxcpm-tts/api-key)" \\
    -v "$PWD/scripts/tts_compare.py:/tts_compare.py:ro" --entrypoint python \\
    gateway_nerves-orchestrator -I /tts_compare.py short --ref http://10.0.0.195:8004

TTS_VOICE = the voice name the servers load (the live deployment's is in .env). --ref is the
server to compare against (Helios before the move); --cand defaults to the Jupiter server.

Modes:
  short   4 short sentences, 3 rounds on each server; prints the median per sentence and the ratio
          (pass: each <= ~1.5x the reference, none over ~2 s).
  long    varied text of ~600 / ~1200 / ~2000 chars, with and without a style note, on the candidate
          only (pass: every run HTTP 200, the ~2000-char ones under ~45 s; the orchestrator gives up
          at 60 s). Varied text on purpose: one repeated sentence makes the model speak far longer
          than normal speech (an 816-char repeat came out as 104 s of audio), which overstates the time.
  listen  writes listen_<name>.wav for each server into --out (mount a directory there) for a
          side-by-side listening test with a style note.

Each server synthesizes one request at a time. Don't run this against a server that is also
serving Jess during busy hours: requests queue behind it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

SHORT = [
    "Good morning, it is seven o'clock.",
    "Your timer for the oven is done.",
    "Here is a quick summary of the weather for today: mild, with some sun in the afternoon.",
    "You have two things on the calendar tomorrow, and the first one starts at nine.",
]
PARAGRAPH = (
    "Good morning. It is a mild day with some sun in the afternoon and a light breeze from the west. "
    "You have two things on the calendar today. The first one starts at nine, and the second one is after lunch. "
    "Remember to drink some water before you head out, and take a short walk if you get the chance. "
    "The recycling goes out tonight, so put the bin by the curb before bed. "
    "Your library books are due on Friday, and the pharmacy said the order is ready for pickup. "
    "In the news, a new bridge opened downtown, the local team won its game last night, and the farmers "
    "market is open on Saturday. Traffic looks normal this morning, with a small delay near the river crossing. "
    "If you want to plan dinner, there are vegetables in the fridge that should be used soon. "
    "That is everything for now. Have a calm and steady day, and take it one step at a time. "
)
STYLE = "calm, gentle and reassuring"
# 16-bit mono PCM at the server's 24 kHz output rate.
BYTES_PER_AUDIO_SECOND = 48000


VOICE = os.getenv("TTS_VOICE", "default")
# The Jupiter server needs its bearer key (/etc/voxcpm-tts/api-key); the Helios rollback has none.
# Sent to the candidate only, never to --ref.
CAND_KEY = os.getenv("TTS_API_KEY", "")


def tts(base: str, text: str, emotion: str = "", timeout: float = 180.0, key: str = "") -> tuple[int, float, bytes]:
    """(HTTP status, seconds, body). Status 0 = no HTTP answer (refused, timed out)."""
    body = {"text": text, "voice": VOICE}
    if emotion:
        body["emotion"] = emotion
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base.rstrip("/") + "/tts", data=json.dumps(body).encode(), headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        data, code = b"", e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        print(f"{base}: no answer ({type(e).__name__})", file=sys.stderr)
        data, code = b"", 0
    return code, time.monotonic() - t0, data


def run_short(ref: str, cand: str) -> int:
    servers = {"ref": ref, "cand": cand}
    times: dict[str, dict[str, list[float]]] = {k: {s: [] for s in SHORT} for k in servers}
    for _ in range(3):
        for which, base in servers.items():
            for s in SHORT:
                code, dt, _ = tts(base, s, timeout=60, key=CAND_KEY if which == "cand" else "")
                if code != 200:
                    print(f"{which} ({base}): HTTP {code}", file=sys.stderr)
                    return 1
                times[which][s].append(dt)
    print(f"{'sentence':<14} {'reference':>9} {'candidate':>9} {'ratio':>6}   candidate runs")
    for i, s in enumerate(SHORT, 1):
        r, c = statistics.median(times["ref"][s]), statistics.median(times["cand"][s])
        runs = " ".join(f"{x:.2f}" for x in times["cand"][s])
        print(f"#{i} ({len(s):>3} ch) {r:>8.2f}s {c:>8.2f}s {c / r:>5.2f}x   {runs}")
    return 0


def run_long(cand: str) -> int:
    failed = 0
    for target in (600, 1200, 2000):
        text = (PARAGRAPH * 3)[:target].rsplit(" ", 1)[0]
        for emotion in ("", STYLE):
            code, dt, data = tts(cand, text, emotion, timeout=180, key=CAND_KEY)
            failed += code != 200
            print(
                f"{len(text):>5} ch style={'yes' if emotion else 'no '}: HTTP {code} {dt:6.1f}s "
                f"audio {len(data) / BYTES_PER_AUDIO_SECOND:5.1f}s"
            )
    return 1 if failed else 0


def run_listen(servers: dict[str, str], out: str) -> int:
    text = "Hi, it's Jess. Your appointment is tomorrow at ten, so leave by nine thirty. You've got this."
    for name, base in servers.items():
        code, _, data = tts(base, text, STYLE, timeout=60, key=CAND_KEY if name == "candidate" else "")
        if code != 200:
            print(f"{name}: HTTP {code}", file=sys.stderr)
            return 1
        with open(f"{out}/listen_{name}.wav", "wb") as fh:
            fh.write(data)
        print(f"wrote {out}/listen_{name}.wav")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("short", "long", "listen"))
    ap.add_argument("--ref", help="reference server, e.g. http://10.0.0.195:8004 (needed for short / listen)")
    ap.add_argument("--cand", default="http://host.docker.internal:8004", help="candidate server (Jupiter)")
    ap.add_argument("--out", default="/out", help="listen mode: directory for the WAV files")
    args = ap.parse_args(argv)
    if args.mode in ("short", "listen") and not args.ref:
        ap.error(f"{args.mode} needs --ref")
    if args.mode == "short":
        return run_short(args.ref, args.cand)
    if args.mode == "long":
        return run_long(args.cand)
    return run_listen({"reference": args.ref, "candidate": args.cand}, args.out)


if __name__ == "__main__":
    sys.exit(main())
