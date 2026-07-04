# Backup & Restore

Jess's `data/` directory is ~1.9 GB, but almost all of that is `hf_cache/` (the
HuggingFace model cache, which re-downloads on demand). The genuinely
**irreplaceable** state is only ~15 MB:

| Path | What | Reconstructable? |
|------|------|------------------|
| `data/app/brain_state.db` | reminders, focus, routines, selfcare, workouts, meals, chat history | ❌ no |
| `data/app/progress.db` | streaks, XP, event history | ❌ no |
| `data/app/finance.db` | budget/finance state | ❌ no |
| `data/app/auto_learn.key` | **Fernet key that decrypts learned personal facts** | ❌ **no — losing it bricks every encrypted memory, even though the ciphertext survives** |
| `data/chroma/` | RAG / mempalace vector store | ⚠️ partially (re-ingest from source) |
| `data/app/` subdirs | self-audits, budget summaries, imports, meal photos, paperless inbox, training corpus, documents | ❌ no |
| `credentials/` | Google OAuth token + client secret | ⚠️ re-auth possible but painful |

Until `scripts/backup_state.py` landed, none of this was backed up — a single
disk failure would have been total and permanent. `hf_cache/`,
`embedding_finetune/`, and prior `app-backup-*/` dirs are deliberately excluded.

## What the backup does

`scripts/backup_state.py` is standalone and **stdlib-only** — no venv, no pip
install, and it runs whether or not the orchestrator container is up:

1. Snapshots each live SQLite DB with the online-backup API
   (`sqlite3.Connection.backup()`), so a WAL database being written to
   *right now* is captured without tearing. (A plain `cp`/`tar` of a live DB can
   grab a corrupt half-write — this avoids that.)
2. Copies the rest of the critical set (key, chroma, subdirs, credentials),
   preserving `auto_learn.key`'s `0600` mode.
3. Writes `backups/jess-state-YYYYmmdd-HHMMSS.tar.gz` (mode `0600` — it contains
   the token and the key).
4. Optionally rsyncs the archive off-box (`JESS_BACKUP_REMOTE`).
5. Rotates: keeps the newest `JESS_BACKUP_KEEP` archives (default 30 — at ~15 MB
   each that's under half a gigabyte for a month of nightlies).
6. Writes a Prometheus textfile metric so **`JessBackupStale` alerts if a
   nightly is missed** (same mechanism as the Google-token refresh).

## Install (on the always-on host — Jupiter)

The script is in the repo; scheduling is a one-time host step. Crontab installs
are blocked by Claude Code's persistence guard, so run this yourself:

```bash
# Nightly at 03:30, with off-box copy + freshness metric.
# Drop the JESS_BACKUP_REMOTE line to keep it local-only.
( crontab -l 2>/dev/null; cat <<'CRON'
30 3 * * * cd /home/labadmin/gateway_nerves && JESS_BACKUP_METRICS_PATH=/home/labadmin/node_exporter/textfile_collector/jess_backup.prom JESS_BACKUP_REMOTE=labadmin@saturn:/home/labadmin/jess-backups /usr/bin/python3 scripts/backup_state.py >> /home/labadmin/gateway_nerves/logs/backup.log 2>&1
CRON
) | crontab -
mkdir -p /home/labadmin/gateway_nerves/logs
```

For the off-box copy, set up key-based SSH to the target first
(`ssh-copy-id labadmin@saturn`) and create the destination dir. Saturn is a good
target — it's a separate always-on-ish box on the tailnet. Leave
`JESS_BACKUP_REMOTE` unset for local-only backups (still far better than none,
but a single-disk loss takes them with the original).

Run it once by hand to verify:

```bash
cd /home/labadmin/gateway_nerves && python3 scripts/backup_state.py
ls -lh backups/
```

## Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `JESS_DATA_DIR` | `<repo>/data` | state to back up |
| `JESS_CREDENTIALS_DIR` | `<repo>/credentials` | OAuth token/secret |
| `JESS_BACKUP_DIR` | `<repo>/backups` | where archives are written |
| `JESS_BACKUP_KEEP` | `30` | archives to retain locally |
| `JESS_BACKUP_REMOTE` | *(unset)* | rsync target for off-box copy |
| `JESS_BACKUP_METRICS_PATH` | *(unset)* | Prometheus textfile for the freshness metric |

Exit codes: `0` = backup written · `1` = nothing to back up · `2` = failed.

## Restore

The archive is a plain `tar.gz` with `data/` and `credentials/` at the top:

```bash
cd /home/labadmin/gateway_nerves
docker compose stop orchestrator            # avoid writing over a restore

# inspect first
tar tzf backups/jess-state-20260704-033000.tar.gz | head

# restore into place (this overwrites current state — be sure)
tar xzf backups/jess-state-20260704-033000.tar.gz -C .

chmod 600 data/app/auto_learn.key credentials/*.json   # tar preserves mode, but confirm
docker compose start orchestrator
```

To restore a single database without touching the rest, extract just that path:
`tar xzf <archive> data/app/brain_state.db`.

> **Test your restore.** A backup you've never restored is a hypothesis. Once a
> quarter, extract the latest archive into a scratch dir and confirm the DBs
> open (`python3 -c "import sqlite3; sqlite3.connect('data/app/brain_state.db').execute('pragma integrity_check')"`).
