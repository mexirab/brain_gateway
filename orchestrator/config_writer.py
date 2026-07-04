"""
Atomic config writers + audit log helper.

All settings PUTs in `routes_config.py` go through this module so that:
- Files are written via tmpfile + os.replace() — no torn writes if the
  process dies mid-flush.
- Every change is recorded in the `config_changes` SQLite table with a
  before/after JSON diff, indexed by panel.

`data_manager.save_medications()` / `save_projects()` also write through
`atomic_write_yaml` (without the audit trail).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# Forward-looking guardrail: today the audit log only carries identity /
# selfcare / quiet-hours / recurring-rule fields, none of which are secret.
# But `log_config_change` happily serializes whatever the route hands it,
# so a future panel could accidentally write tokens/passwords into a
# plaintext SQLite column. Mask any key that looks credential-shaped.
_REDACT_KEY_RE = re.compile(r"(token|secret|password|api[_-]?key|auth)", re.IGNORECASE)


def _redact(value: Any) -> Any:
    """Recursively replace credential-shaped values with a placeholder."""
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if _REDACT_KEY_RE.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def atomic_write_yaml(path: str | Path, data: Dict[str, Any]) -> None:
    """Write a YAML file atomically.

    Strategy: dump to a tmpfile in the same directory (so os.replace is
    on the same filesystem and stays atomic), fsync, then replace. If
    anything raises, the original file is untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Cleanup tmpfile on failure; original file unaffected
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def log_config_change(
    panel: str,
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    changed_by: str = "user",
) -> None:
    """Append a row to the config_changes audit table.

    Failures are logged but never raised — audit drift must not break a
    user's settings save.
    """
    try:
        from orchestrator.state_store import get_db

        diff = {"before": _redact(before), "after": _redact(after)}
        with get_db() as conn:
            conn.execute(
                """INSERT INTO config_changes (changed_at, panel, diff_json, changed_by)
                   VALUES (?, ?, ?, ?)""",
                (datetime.now().isoformat(), panel, json.dumps(diff, default=str), changed_by),
            )
    except Exception as e:
        logger.warning(f"[CONFIG_WRITER] Failed to log audit row for {panel}: {e}")
