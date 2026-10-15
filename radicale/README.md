# radicale/

Self-hosted CalDAV server (Radicale) on **Jupiter**, the personal calendar source of
truth. It's its own compose project, like `homeassistant/`; the orchestrator's
build/deploy never touches it.

| File | What |
|------|------|
| `docker-compose.yml` | Radicale 3.8.3 pinned by digest. Published on `127.0.0.1` only (Tailscale Serve `:5233`); containers use `radicale:5232` over gateway-net / monitoring-net. |
| `config/config` | Server config: htpasswd/bcrypt auth, info-level logging, sharing off. |
| `config/rights` | Access rules: `owner` has full control; `jess` can read/write events only. |
| `backup_radicale.sh` | Nightly backup → Saturn + Prometheus freshness metric. |

Runbook (users, iPhone setup, upgrade, backup/restore, known gaps): [`docs/CALENDAR.md`](../docs/CALENDAR.md).
