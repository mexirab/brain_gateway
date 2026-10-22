"""
Grist (self-hosted spreadsheet) client for the `spreadsheet` tool.

Grist runs on Jupiter (grist/docker-compose.yml) as the local replacement for
Google Sheets. Jess only reaches documents named in GRIST_DOCS
(``nickname=docId,...``): the model says "gym", never a raw doc id, and a doc
not on the list cannot be read or written however the model asks. (Use a
least-privilege Grist user's API key too, so Grist enforces the same list.)

Rows go in and out keyed by column LABEL ("Weight (lbs)"), not Grist's column
id ("Weight_lbs"): labels are what the user sees and what the model will say.
Formula columns are readable but never writable. No delete in this version.
Reads come back newest-first from Grist and are returned oldest→newest; Date /
DateTime cells are converted from Grist's epoch seconds to ISO text, and an
ISO date in a filter on a Date column is converted back.

Never raises: every entry point returns a dict ({"success": True, ...} or
{"success": False, "error": reason, ...}) and bumps
``bgw_spreadsheet_total{action,result}`` exactly once. Cell contents are the
user's (or anyone they share a sheet with) — callers frame them as untrusted
data, and nothing here logs a cell, a filter value or a column name.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from orchestrator.config import parse_grist_docs
from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)

# Up to 3 requests per tool call must fit under the handler's 20 s ceiling.
_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_READ_ROWS = 50
MAX_WRITE_ROWS = 20
_MAX_CELL_IN = 1000  # chars per written cell


def allowed_docs() -> Dict[str, str]:
    return parse_grist_docs(_settings.grist_docs)


def _count(action: str, result: str) -> None:
    from orchestrator.metrics import SPREADSHEET_TOTAL

    SPREADSHEET_TOTAL.labels(action=action, result=result).inc()


def _fail(action: str, reason: str, exc: Optional[Exception] = None, **extra: Any) -> Dict[str, Any]:
    _count(action, reason)
    # Type name only: an httpx error string carries the request URL, which
    # carries the filter values.
    logger.warning("[GRIST] %s failed: %s", action, type(exc).__name__ if exc else reason)
    return {"success": False, "error": reason, **extra}


def _exc_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    return "other"


def _status_reason(code: int) -> str:
    if code in (401, 403):
        return "auth"
    if code == 404:
        return "not_found"
    if 400 <= code < 500:
        return "http_4xx"
    return "http_5xx" if code >= 500 else "http_other"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_settings.grist_url.rstrip("/"),
        timeout=_TIMEOUT,
        headers={"Authorization": f"Bearer {_settings.grist_api_key}", "Accept": "application/json"},
        follow_redirects=False,
    )


def _resolve(action: str, sheet: str, table: Optional[str] = None) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Validate the sheet nickname (and table id). Returns (doc_id, None) or (None, failure)."""
    if not _settings.grist_enabled:
        return None, _fail(action, "disabled")
    docs = allowed_docs()
    doc_id = docs.get(str(sheet or "").strip().lower())
    if not doc_id:
        return None, _fail(action, "unknown_sheet", sheets=sorted(docs))
    if table is not None and not _TABLE_RE.match(str(table)):
        return None, _fail(action, "bad_table")
    return doc_id, None


def _doc_path(doc_id: str, *parts: str) -> str:
    return "/api/docs/" + "/".join(quote(p, safe="") for p in (doc_id, *parts))


async def _get_json(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> tuple[int, Any]:
    resp = await client.get(path, params=params)
    if resp.status_code != 200:
        return resp.status_code, None
    return 200, resp.json()


async def _columns(client: httpx.AsyncClient, doc_id: str, table: str) -> tuple[int, List[Dict[str, Any]]]:
    """User-visible columns of a table: [{id, label, type, formula}] (helpers dropped)."""
    code, body = await _get_json(client, _doc_path(doc_id, "tables", table, "columns"))
    if code != 200:
        return code, []
    cols = []
    for c in (body or {}).get("columns", []) if isinstance(body, dict) else []:
        cid = c.get("id") if isinstance(c, dict) else None
        if not isinstance(cid, str) or cid.startswith("gristHelper_") or cid == "manualSort":
            continue
        f = c.get("fields") if isinstance(c.get("fields"), dict) else {}
        cols.append(
            {
                "id": cid,
                "label": str(f.get("label") or cid)[:100],
                "type": str(f.get("type") or "Any")[:60],
                "formula": bool(f.get("isFormula")) and bool(f.get("formula")),
            }
        )
    return 200, cols


def _by_label(cols: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    k = str(key).strip().lower()
    for c in cols:
        if c["label"].lower() == k or c["id"].lower() == k:
            return c
    return None


def _display(value: Any, col_type: str) -> Any:
    """Grist's stored value → something the model can read: Date/DateTime
    epoch seconds → ISO text, error cells → "(error)", lists flattened."""
    if isinstance(value, list) and value:
        if value[0] == "E":
            return "(error)"
        if value[0] == "L":
            return ", ".join(str(v) for v in value[1:])
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if col_type == "Date":
                return _dt.datetime.fromtimestamp(value, _dt.UTC).date().isoformat()
            if col_type.startswith("DateTime"):
                tz_name = col_type.partition(":")[2] or "UTC"
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:  # noqa: BLE001 — unknown zone name → UTC
                    tz = _dt.UTC
                return _dt.datetime.fromtimestamp(value, tz).strftime("%Y-%m-%d %H:%M %Z")
        except (OverflowError, OSError, ValueError):
            return value
    return value


def _filter_value(value: Any, col_type: str) -> Any:
    """An ISO date filtering a Date column → Grist's epoch seconds."""
    if col_type == "Date" and isinstance(value, str) and _ISO_DATE_RE.match(value.strip()):
        try:
            d = _dt.date.fromisoformat(value.strip())
            return int(_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.UTC).timestamp())
        except ValueError:
            return value
    return value


def _to_fields(cols: List[Dict[str, Any]], values: Any) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """{label: value} → {col_id: value}. Returns (fields, None) or (None, reason).
    Strings are sent as-is: Grist parses "2026-10-22" into a Date column itself."""
    if not isinstance(values, dict) or not values:
        return None, "no_values"
    fields: Dict[str, Any] = {}
    for key, val in values.items():
        col = _by_label(cols, key)
        if col is None:
            return None, "unknown_column"
        if col["formula"]:
            return None, "formula_column"
        if isinstance(val, str):
            val = val[:_MAX_CELL_IN]
        elif not (val is None or isinstance(val, (bool, int, float))):
            return None, "bad_value"
        fields[col["id"]] = val
    return fields, None


def _labelled(record: Any, cols: List[Dict[str, Any]]) -> Dict[str, Any]:
    fields = record.get("fields") if isinstance(record, dict) else None
    if not isinstance(fields, dict):
        return {}
    return {c["label"]: _display(fields[c["id"]], c["type"]) for c in cols if c["id"] in fields}


def _writable(cols: List[Dict[str, Any]]) -> List[str]:
    return [c["label"] for c in cols if not c["formula"]]


async def list_sheets() -> Dict[str, Any]:
    """{"success": True, "sheets": [{"sheet": nick, "tables": [table ids]}]}.
    A doc that can't be listed is reported with an error instead of tables
    (and the call counts as ``partial``, so a revoked key shows in metrics)."""
    if not _settings.grist_enabled:
        return _fail("list", "disabled")
    out = []
    try:
        async with _client() as client:
            for nick, doc_id in sorted(allowed_docs().items()):
                code, body = await _get_json(client, _doc_path(doc_id, "tables"))
                if code != 200 or not isinstance(body, dict):
                    out.append({"sheet": nick, "error": _status_reason(code) if code != 200 else "bad_body"})
                    continue
                tables = [
                    t["id"]
                    for t in body.get("tables", [])
                    if isinstance(t, dict)
                    and isinstance(t.get("id"), str)
                    and not (t.get("fields") or {}).get("summarySourceTable")
                ]
                out.append({"sheet": nick, "tables": tables[:30]})
    except Exception as exc:  # noqa: BLE001 — never raises; metric fires once
        return _fail("list", _exc_reason(exc), exc)
    failed = sum(1 for s in out if "error" in s)
    _count("list", "partial" if failed else "ok")
    logger.info("[GRIST] list ok (%d sheets, %d failed)", len(out), failed)
    return {"success": True, "sheets": out}


async def read_rows(
    sheet: str, table: str, filters: Optional[Dict[str, Any]] = None, limit: int = 20
) -> Dict[str, Any]:
    """The newest ``limit`` rows of one table (oldest→newest), keyed by column label.

    ``filters`` is {label: value or [values]} (exact match, Grist semantics).
    Returns {"success": True, "columns": [...], "rows": [{"id", "fields"}],
    "truncated": bool} — truncated means older rows exist.
    """
    doc_id, failure = _resolve("read", sheet, table)
    if failure:
        return failure
    try:
        limit = max(1, min(int(limit or 20), MAX_READ_ROWS))
    except (TypeError, ValueError):
        limit = 20
    try:
        async with _client() as client:
            code, cols = await _columns(client, doc_id, table)
            if code != 200:
                return _fail("read", _status_reason(code))
            params: Dict[str, Any] = {"limit": limit + 1, "sort": "-id"}
            if filters:
                if not isinstance(filters, dict):
                    return _fail("read", "bad_filter")
                grist_filter = {}
                for key, val in filters.items():
                    col = _by_label(cols, key)
                    if col is None:
                        return _fail("read", "unknown_column", columns=[c["label"] for c in cols])
                    vals = val if isinstance(val, list) else [val]
                    usable = [
                        _filter_value(v, col["type"])
                        for v in vals
                        if v is None or isinstance(v, (str, int, float, bool))
                    ][:20]
                    if not usable:
                        return _fail("read", "bad_filter")
                    grist_filter[col["id"]] = usable
                params["filter"] = json.dumps(grist_filter)
            code, body = await _get_json(client, _doc_path(doc_id, "tables", table, "records"), params)
            if code != 200:
                return _fail("read", _status_reason(code))
            records = body.get("records") if isinstance(body, dict) else None
            records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
            rows = [{"id": r.get("id"), "fields": _labelled(r, cols)} for r in records[:limit]]
            rows.reverse()
    except Exception as exc:  # noqa: BLE001
        return _fail("read", _exc_reason(exc), exc)
    _count("read", "ok")
    logger.info("[GRIST] read ok (%d rows)", len(rows))
    return {"success": True, "columns": cols, "rows": rows, "truncated": len(records) > limit}


async def add_rows(sheet: str, table: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Append rows ({label: value} each). Returns {"success": True, "ids": [...]}.

    A failure after the POST was sent is reported as ``write_unknown``: Grist
    may have saved the rows, so the caller must not blindly retry.
    """
    doc_id, failure = _resolve("add", sheet, table)
    if failure:
        return failure
    if not isinstance(rows, list) or not rows:
        return _fail("add", "no_values")
    if len(rows) > MAX_WRITE_ROWS:
        return _fail("add", "too_many_rows")
    sent = False
    try:
        async with _client() as client:
            code, cols = await _columns(client, doc_id, table)
            if code != 200:
                return _fail("add", _status_reason(code))
            records = []
            for values in rows:
                fields, reason = _to_fields(cols, values)
                if reason:
                    return _fail("add", reason, columns=_writable(cols))
                records.append({"fields": fields})
            sent = True
            resp = await client.post(_doc_path(doc_id, "tables", table, "records"), json={"records": records})
            if resp.status_code != 200:
                # A 5xx (or proxy 504) can arrive after Grist already saved.
                reason = "write_unknown" if resp.status_code >= 500 else _status_reason(resp.status_code)
                return _fail("add", reason)
            body = resp.json()
            created = body.get("records") if isinstance(body, dict) else None
            ids = [r.get("id") for r in created if isinstance(r, dict)] if isinstance(created, list) else []
    except Exception as exc:  # noqa: BLE001
        return _fail("add", "write_unknown" if sent else _exc_reason(exc), exc)
    _count("add", "ok")
    logger.info("[GRIST] add ok (%d rows)", len(ids))
    return {"success": True, "ids": ids}


async def update_row(sheet: str, table: str, row_id: int, values: Dict[str, Any]) -> Dict[str, Any]:
    """Change cells of one existing row ({label: value}).

    Returns {"success": True, "id": n, "before": {label: old value}} so the
    change (and what it overwrote) is visible in the chat.
    """
    doc_id, failure = _resolve("update", sheet, table)
    if failure:
        return failure
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return _fail("update", "bad_row_id")
    if row_id <= 0:
        return _fail("update", "bad_row_id")
    sent = False
    try:
        async with _client() as client:
            code, cols = await _columns(client, doc_id, table)
            if code != 200:
                return _fail("update", _status_reason(code))
            fields, reason = _to_fields(cols, values)
            if reason:
                return _fail("update", reason, columns=_writable(cols))
            # Check the row exists first so a wrong id gets a clear answer.
            code, body = await _get_json(
                client,
                _doc_path(doc_id, "tables", table, "records"),
                {"filter": json.dumps({"id": [row_id]})},
            )
            if code != 200:
                return _fail("update", _status_reason(code))
            existing = body.get("records") if isinstance(body, dict) else None
            if not isinstance(existing, list) or not existing:
                return _fail("update", "row_not_found")
            changed = [c for c in cols if c["id"] in fields]
            before = _labelled(existing[0], changed)
            sent = True
            resp = await client.patch(
                _doc_path(doc_id, "tables", table, "records"),
                json={"records": [{"id": row_id, "fields": fields}]},
            )
            if resp.status_code != 200:
                reason = "write_unknown" if resp.status_code >= 500 else _status_reason(resp.status_code)
                return _fail("update", reason)
    except Exception as exc:  # noqa: BLE001
        return _fail("update", "write_unknown" if sent else _exc_reason(exc), exc)
    _count("update", "ok")
    logger.info("[GRIST] update ok (%d cells)", len(fields))
    return {"success": True, "id": row_id, "before": before}
