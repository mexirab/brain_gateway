# Backup & Restore

Jess's `data/` directory is ~1.9 GB, but almost all of that is `hf_cache/` (the
HuggingFace model cache, which re-downloads on demand). The genuinely
**irreplaceable** state is only ~15 MB:

| Path | What | Reconstructable? |
|------|------|------------------|
| `data/app/brain_state.db` | reminders, focus, routines, selfcare, workouts, meals, pantry inventory, chat history | ❌ no |
| `data/app/progress.db` | streaks, XP, event history | ❌ no |
| `data/app/finance.db` | budget/finance state | ❌ no |
| `data/app/auto_learn.key` | **Fernet key that decrypts learned personal facts** | ❌ **no — losing it bricks every encrypted memory, even though the ciphertext survives** |
| `data/app/webpush_vapid.pem` | VAPID private key for dashboard Web Push (`WEBPUSH_KEY_PATH`, 0600). Critical file — an unreadable copy fails the run | ⚠️ a new key is auto-created, but every subscribed device stops receiving pushes until re-enabled in Settings → Notifications. The archive (and its plaintext Saturn copy) holds it unencrypted — anyone with the archive can sign pushes to subscribed devices |
| live Chroma store (`CHROMA_HOST_PATH`, archived as `chroma/`) | RAG / mempalace vector store, incl. auto-learned facts | ⚠️ RAG docs re-ingestable; auto-learned facts are ❌ not |
| `data/app/` subdirs | self-audits, budget summaries, imports, meal photos, paperless inbox, training corpus | ❌ no |
| `data/documents/` | document vault uploads (`DOCUMENTS_PATH`, default `./data/documents`) — the original files; `brain_state.db` and Chroma only hold their extracted text. Archived (and copied to Saturn) unencrypted, like the rest. Only covered while `DOCUMENTS_PATH` stays under `data/` — the script does not follow a custom path | ❌ no |
| `credentials/` | Google OAuth token + client secret | ⚠️ re-auth possible but painful |

Until `scripts/backup_state.py` landed, none of this was backed up — a single
disk failure would have been total and permanent. `hf_cache/`,
`embedding_finetune/`, and prior `app-backup-*/` dirs are deliberately excluded,
as are `exercise_library/` (+ its `.new`/`.old` fetch siblings) — ~100 MB of
public-domain photos; re-fetch with
`docker exec brain-orchestrator python -m orchestrator.exercise_library fetch`.
`pantry_photos/` is also excluded: transient stock-take photos of the inside of
the home, deleted on commit/discard and by the daily `pantry_sweep` — never
worth keeping in 30 rotated archives on two machines (see `docs/PANTRY.md`).
Receipt photos follow the same rule, and committed/discarded receipt scans keep
only store/date/total in `brain_state.db` — no line-level purchase history
reaches the backups.

## What the backup does

`scripts/backup_state.py` is standalone and **stdlib-only** — no venv, no pip
install, and it runs whether or not the orchestrator container is up:

1. Snapshots each live SQLite DB with the online-backup API
   (`sqlite3.Connection.backup()`), so a WAL database being written to
   *right now* is captured without tearing. (A plain `cp`/`tar` of a live DB can
   grab a corrupt half-write — this avoids that.)
2. Copies the rest of the critical set (key, subdirs, credentials, and the
   LIVE Chroma store), preserving `auto_learn.key`'s `0600` mode. The Chroma
   dir resolves from `JESS_CHROMA_DIR`, else `CHROMA_HOST_PATH` read from the
   repo `.env` (what compose mounts at `/chroma` — cron doesn't load `.env`),
   else `data/chroma`; staged once (not duplicated) when it lives under `data/`.
   Its `chroma.sqlite3` is snapshotted like the other DBs; the HNSW `.bin`
   index files are copied raw. Before 2026-10-02 the archive only held a stale
   April `data/chroma` copy and none of the auto-learned facts.
   Missing Chroma dir logs a WARN; an unreadable critical file fails the run.
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
| `JESS_CHROMA_DIR` | `CHROMA_HOST_PATH` from `<repo>/.env`, else `<repo>/data/chroma` | live Chroma (memory palace) store |
| `JESS_BACKUP_DIR` | `<repo>/backups` | where archives are written |
| `JESS_BACKUP_KEEP` | `30` | archives to retain locally |
| `JESS_BACKUP_REMOTE` | *(unset)* | rsync target for off-box copy |
| `JESS_BACKUP_METRICS_PATH` | *(unset)* | Prometheus textfile for the freshness metric |

Exit codes: `0` = backup written · `1` = nothing to back up · `2` = failed.

## Restore

The archive is a plain `tar.gz` with `data/`, `credentials/`, and (when the
live store is outside `data/`) `chroma/` at the top:

```bash
cd /home/labadmin/gateway_nerves
docker compose stop orchestrator            # avoid writing over a restore

# inspect first
tar tzf backups/jess-state-20260704-033000.tar.gz | head

# restore into place (this overwrites current state — be sure)
tar xzf backups/jess-state-20260704-033000.tar.gz -C .

chmod 600 data/app/auto_learn.key data/app/webpush_vapid.pem credentials/*.json   # tar preserves mode, but confirm
docker compose start orchestrator
```

To restore a single database without touching the rest, extract just that path:
`tar xzf <archive> data/app/brain_state.db`.

**Chroma:** `chroma/` extracts into the repo dir — copy its contents to the
live `CHROMA_HOST_PATH` with the orchestrator stopped. The sqlite is a
consistent snapshot, but the HNSW `.bin` files were copied raw while live and
may be inconsistent with it; if searches error or miss recent memories after a
restore, rebuild the index (the sqlite is the source of truth).

## Actual Budget (finance source of truth)

Separate nightly job for the Actual Budget server (`actual/` compose project,
container `actual-budget`, data at `/opt/apps/actual/data`, owned `1000:1000`
mode `700`). Not covered by `backup_state.py`, which only holds the
orchestrator's `finance.db` mirror.

| | |
|---|---|
| Script | `actual/backup_actual.sh` (bash + stdlib python, runs as the host user — no sudo) |
| Schedule | cron 03:55 on Jupiter |
| What | every `*.sqlite` via the online-backup API + `PRAGMA quick_check` (fails the run on error); all other files (budget blobs) copied, retried once if they change mid-read; symlinks skipped |
| Local | `/home/labadmin/actual/backups/actual-YYYYmmdd-HHMMSS.tar.gz` (mode 600), keep 30. A one-off `pre-compose-*.tar.gz` rollback snapshot there is not rotated. |
| Off-box | rsync to `saturn:/home/labadmin/actual-backups/` (`JESS_ACTUAL_BACKUP_REMOTE`; plaintext, like the other backups) |
| Log | `/home/labadmin/actual/backup.log` |
| Metrics | textfile `jess_actual_backup.prom`: `jess_actual_backup_success_timestamp_seconds`, `jess_actual_backup_offbox_success`, `jess_actual_backup_files` |
| Alerts | `JessActualBackupStale` (>36h, paging), `JessBackupOffboxFailing` (shared, quiet) |

Overrides: `JESS_ACTUAL_DATA`, `JESS_ACTUAL_BACKUP_REMOTE`,
`JESS_ACTUAL_BACKUP_METRICS_PATH`.

**Restore** (archive has `data/` at the top):

```bash
cd /home/labadmin/gateway_nerves/actual && docker compose down
tar tzf /home/labadmin/actual/backups/actual-<stamp>.tar.gz | head   # inspect
mkdir -p /tmp/actual-restore && tar xzf /home/labadmin/actual/backups/actual-<stamp>.tar.gz -C /tmp/actual-restore
sudo rsync -a --delete /tmp/actual-restore/data/ /opt/apps/actual/data/
sudo chown -R 1000:1000 /opt/apps/actual/data && sudo chmod 700 /opt/apps/actual/data
docker compose up -d
rm -rf /tmp/actual-restore
```

Verify: `curl -s -H "Authorization: Bearer $API_TOKEN" http://localhost:8888/api/finance/sync/status`
(or trigger a sync) and open the Actual UI at https://jupiter-amds.tail74fc4a.ts.net:5007 (tailnet only).

## File ownership

The orchestrator container runs as non-root `jess` with uid/gid
`ORCHESTRATOR_UID`/`ORCHESTRATOR_GID` (default 1000, the installing user), so
everything it writes under `data/app/` is owned by the same user the backup
cron runs as. `config_writer.match_parent_owner` is a no-op by default (kept
only for root-run deployments). Hosts that ran the old root container may
still have root-owned files (including SQLite `*-wal`/`*-shm`/`*-journal`
sidecars): `bash scripts/orchestrator_state_perms.sh` reports them, `--apply`
chowns them.

> **Test your restore.** A backup you've never restored is a hypothesis. Once a
> quarter, extract the latest archive into a scratch dir and confirm the DBs
> open (`python3 -c "import sqlite3; sqlite3.connect('data/app/brain_state.db').execute('pragma integrity_check')"`).
