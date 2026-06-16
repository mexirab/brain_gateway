# Demo GIF — capture script

The README leads with a 30-second demo. This doc specs that capture so anyone
can record it consistently. The goal: show the **single most ADHD-resonant
loop** — *thought leaves your head → the assistant catches it → it comes back to
you at the right moment, one tap to clear it* — in under 30 seconds, with no
narration needed.

## Where it slots in

The README has a placeholder block under **"See it in 30 seconds"**:

```markdown
![Brain dump → reminder fires → one-tap Done/Snooze on your phone](docs/img/demo.gif)
```

Drop the finished file at **`docs/img/demo.gif`** and delete the
"_Recording pending_" note beneath it. That's the only wiring needed — the
`<!-- DEMO GIF: ... -->` comment marks the exact spot.

## The story (3 beats, 15–30s total)

A single continuous take is best, but a 3-clip stitch is fine. Each beat earns
its seconds — don't pad.

| Beat | ~Seconds | What's on screen | What it proves |
|------|----------|------------------|----------------|
| **1. Brain dump** | 0–8s | User taps the mic (or types) and dumps a messy, run-on thought: *"ok remind me to email the landlord about the leak tomorrow at 9, and I keep forgetting to take my meds, and add oat milk to the grocery list."* The assistant replies confirming it split that into a reminder, a recurring nudge, and a list item. | Low-activation capture — you don't have to be organized to use it. |
| **2. Reminder fires** | 8–18s | Cut to the right moment (or a sped-up clock). The reminder triggers — show the dashboard toast/voice indicator **and** the phone lock screen banner ("Email the landlord about the leak"). | It actually comes back to you, on the channel you'll see. |
| **3. One-tap Done** | 18–28s | On the phone banner, tap **Done** (or **Snooze**). Cut back to the dashboard showing it cleared / the streak ticking up. | Closing the loop is one tap — no app to open, no friction. |

End on the cleared state, not a menu. The last frame should feel like *relief*.

## Capture settings

- **Resolution:** 1280×720 or 1080×1350 (portrait reads well for the phone beats). Keep the file readable on a phone — README viewers are often mobile.
- **Frame rate:** 12–15 fps is plenty for a GIF and keeps the size down.
- **Length:** 15–30s. Hard cap 30s — attention is the whole point.
- **File size:** target **< 5 MB**, hard ceiling 10 MB. GitHub renders inline up to ~10 MB but a heavy GIF hurts the very people this is for. If it's too big: drop fps to 10, trim dead frames, cap width at 800px, or reduce the palette to 64 colors.
- **Format:** GIF for the README inline. Also keep a higher-quality MP4/WebM source for the release page and social — but the README points at the GIF.

## Privacy checklist before publishing

This is a public asset on a public repo. Before committing the GIF:

- [ ] **No real LAN IPs / hostnames** — no `10.0.0.x`, no Tailscale `*.ts.net`, no `helios`/`labadmin`. Use `localhost` or a generic `your-box` in any visible URL bar.
- [ ] **No real personal data** — use throwaway sample content (the landlord/meds/oat-milk script above is safe filler). No real names, addresses, calendar entries, or contacts on screen.
- [ ] **No tokens / secrets** — no `DASHBOARD_TOKEN`, `API_TOKEN`, or API keys visible in any terminal, `.env`, or URL.
- [ ] **Neutral persona** — the assistant is "Jess"; do not show the real-person voice-clone branding (see `docs/internal/` notes on what stays local).
- [ ] **Generic device** — phone screenshots shouldn't expose other notifications, wallpapers with personal info, etc.

## Tooling suggestions

Any of these works — pick what you have:

- **macOS:** screen-record with QuickTime / `⌘⇧5`, mirror the phone via QuickTime (Lightning) or scrcpy (Android), then convert: `ffmpeg -i demo.mov -vf "fps=12,scale=800:-1:flags=lanczos" -c:v gif docs/img/demo.gif` (or use [Gifski](https://gif.ski) for a better palette).
- **Linux:** [Peek](https://github.com/phw/peek) records straight to GIF; or `ffmpeg` as above.
- **Phone beats:** record the lock-screen push separately and stitch — easier than capturing a real push live. A staged/mock push is fine as long as the text matches a reminder the dashboard actually shows.

Keep the dashboard in a clean state (no leftover test reminders cluttering the
view) before you hit record.
</content>
