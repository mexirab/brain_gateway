"""
In-process RAG source file ingestion for the orchestrator.

Re-ingests changed text files from the RAG source directory into
`shared.collection` on a schedule, using the orchestrator's existing
chromadb client and embedding model.

Why in-process (not a subprocess or sidecar container):

- **No HNSW staleness.** When a separate chromadb client writes to a
  persistent collection, the long-running orchestrator's cached HNSW
  index doesn't see the new segments until its client is recreated.
  That forces a full daemon restart after every ingest, which is
  unacceptable for a feature that runs automatically on file edits.
  Doing the upsert through `shared.collection` avoids this entirely —
  the writes are immediately visible to the same process's queries.

- **No duplicate model loading.** `sentence_transformers` + the
  embedding model use hundreds of megabytes and several seconds to load.
  The orchestrator already has `shared.embedding_model` loaded at
  startup, so reusing it is free.

- **No extra container to operate.** One less thing to monitor.

Logic is ported from `rag/ingest_rag.py` and uses the same file-hash
delta strategy: only files whose SHA-256 hash differs from the stored
`file_marker` get re-chunked and re-embedded.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from orchestrator import shared

logger = logging.getLogger(__name__)

# --- Constants (mirror rag/ingest_rag.py) ------------------------------------

_TEXT_EXTS = {".md", ".txt", ".log", ".rst"}
_IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)\s*$", re.MULTILINE)
_CHUNK_TARGET_CHARS = 2400
_CHUNK_OVERLAP_CHARS = 300
_UPSERT_BATCH = 256
_SCAN_LIMIT = 500

# Source dir (container-side path from the bind mount in docker-compose)
_RAG_SOURCE = Path(os.environ.get("RAG_INGEST_SOURCE", "/rag"))

# Module-level tracking of when we last ran a full ingest so we can skip
# cheap "nothing changed" scans without waking the embedding model.
_last_ingest_mtime: float = 0.0
_last_ingest_time: float = 0.0


# --- Hash, file walking, chunking helpers ------------------------------------


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def _iter_text_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir() and p.name in _IGNORE_DIRS:
            continue
        if p.is_file() and p.suffix.lower() in _TEXT_EXTS:
            yield p


def _split_markdown_by_headers(text: str) -> List[Tuple[str, str]]:
    """Split markdown at headers, prefixing each chunk with parent headers."""
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return [("document", text)]

    parent_headers: Dict[int, str] = {}
    sections: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        lvl = len(m.group(1))
        title = m.group(2).strip()
        body = text[m.end() : end].strip()
        header_line = text[m.start() : m.end()].strip()

        parent_headers[lvl] = header_line
        for existing_lvl in list(parent_headers.keys()):
            if existing_lvl > lvl:
                del parent_headers[existing_lvl]

        context_parts = [parent_headers[l] for l in sorted(parent_headers.keys()) if l < lvl]
        if context_parts:
            chunk_content = "\n".join(context_parts) + f"\n\n{header_line}\n\n{body}"
        else:
            chunk_content = f"{header_line}\n\n{body}"

        sections.append((f"h{lvl}:{title}", chunk_content.strip()))
    return sections


def _chunk_text(text: str) -> List[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks: List[str] = []
    i, n = 0, len(text)
    while i < n:
        j = min(i + _CHUNK_TARGET_CHARS, n)
        cut = text.rfind("\n\n", i, j)
        if cut != -1 and cut > i + _CHUNK_TARGET_CHARS * 0.6:
            j = cut
        piece = text[i:j].strip()
        if piece:
            chunks.append(piece)
        if j >= n:
            break
        i = max(0, j - _CHUNK_OVERLAP_CHARS)
    return chunks


def _delete_by_file_path(file_path: str) -> int:
    """Delete every chunk in shared.collection whose metadata.file_path matches."""
    deleted = 0
    coll = shared.collection
    while True:
        res = coll.get(where={"file_path": file_path}, include=[], limit=_SCAN_LIMIT)
        ids = res.get("ids", [])
        if not ids:
            break
        coll.delete(ids=ids)
        deleted += len(ids)
        # After delete, the remaining pages shift — restart from the beginning
    return deleted


def _newest_source_mtime() -> float:
    """Return the newest mtime of any text file under RAG_SOURCE, or 0."""
    if not _RAG_SOURCE.is_dir():
        return 0.0
    newest = 0.0
    try:
        for f in _iter_text_files(_RAG_SOURCE):
            try:
                m = f.stat().st_mtime
                if m > newest:
                    newest = m
            except OSError:
                continue
    except Exception as e:
        logger.warning("[RAG_INGEST] mtime scan failed: %s", e)
    return newest


# --- Core sync ingest --------------------------------------------------------


def _run_ingest_sync() -> Dict[str, int]:
    """Synchronous, in-process ingest using shared.collection + embedding_model.

    Returns a stats dict: changed_files, deleted_chunks, new_chunks, total.
    Safe to call from `asyncio.to_thread`.
    """
    stats: Dict[str, int] = {
        "changed_files": 0,
        "deleted_chunks": 0,
        "new_chunks": 0,
        "total": 0,
    }

    if not _RAG_SOURCE.is_dir():
        logger.warning("[RAG_INGEST] Source dir missing: %s", _RAG_SOURCE)
        return stats

    coll = shared.collection
    model = shared.embedding_model

    # 1. Load existing file markers into a hash map for delta detection
    existing_hash_by_rel: Dict[str, str] = {}
    offset = 0
    while True:
        res = coll.get(
            where={"kind": "file_marker"},
            include=["metadatas"],
            limit=_SCAN_LIMIT,
            offset=offset,
        )
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        if not ids:
            break
        for doc_id, meta in zip(ids, metas):
            if doc_id.startswith("file::") and meta and "file_path" in meta:
                existing_hash_by_rel[meta["file_path"]] = meta.get("file_hash", "")
        offset += len(ids)

    # 2. Walk the source dir and build upsert batches for changed files
    upsert_ids: List[str] = []
    upsert_docs: List[str] = []
    upsert_metas: List[Dict[str, Any]] = []
    seen = set()

    for f in _iter_text_files(_RAG_SOURCE):
        rel = str(f.relative_to(_RAG_SOURCE))
        seen.add(rel)

        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("[RAG_INGEST] Failed to read %s: %s", rel, e)
            continue

        h = _sha256_text(raw)
        if existing_hash_by_rel.get(rel) == h:
            continue  # unchanged — skip

        # Changed or new: clear the old chunks and marker, queue new ones
        stats["deleted_chunks"] += _delete_by_file_path(rel)
        try:
            coll.delete(ids=[f"file::{rel}"])
        except Exception:
            pass
        stats["changed_files"] += 1

        sections = (
            _split_markdown_by_headers(raw)
            if f.suffix.lower() == ".md"
            else [("document", raw)]
        )
        filehash_prefix = h[:12]
        chunk_count = 0
        for sec_title, sec_text in sections:
            for k, piece in enumerate(_chunk_text(sec_text)):
                upsert_ids.append(f"chunk::{rel}::{sec_title}::{k}::{filehash_prefix}")
                upsert_docs.append(piece)
                upsert_metas.append(
                    {
                        "file_path": rel,
                        "file_hash": h,
                        "section": sec_title,
                        "chunk_index": k,
                        "source_root": str(_RAG_SOURCE),
                        "kind": "chunk",
                    }
                )
                chunk_count += 1

        # File marker — kept in sync with the chunks so delta detection works
        upsert_ids.append(f"file::{rel}")
        upsert_docs.append(f"FILE MARKER\npath={rel}\nhash={h}\nchunks={chunk_count}")
        upsert_metas.append(
            {
                "file_path": rel,
                "file_hash": h,
                "kind": "file_marker",
                "chunk_count": chunk_count,
            }
        )

    # 3. Handle deleted files: markers exist but source file is gone
    removed = set(existing_hash_by_rel.keys()) - seen
    for rel in removed:
        stats["deleted_chunks"] += _delete_by_file_path(rel)
        try:
            coll.delete(ids=[f"file::{rel}"])
        except Exception:
            pass
        stats["changed_files"] += 1

    # 4. Batch upsert
    if upsert_ids:
        for i in range(0, len(upsert_ids), _UPSERT_BATCH):
            b_ids = upsert_ids[i : i + _UPSERT_BATCH]
            b_docs = upsert_docs[i : i + _UPSERT_BATCH]
            b_metas = upsert_metas[i : i + _UPSERT_BATCH]
            embeddings = model.encode(b_docs, normalize_embeddings=True).tolist()
            coll.upsert(
                ids=b_ids,
                documents=b_docs,
                metadatas=b_metas,
                embeddings=embeddings,
            )
        stats["new_chunks"] = len(upsert_ids)

    stats["total"] = coll.count()
    return stats


# --- Scheduled job entry point ----------------------------------------------


async def check_and_ingest():
    """APScheduler job: scan mtimes, run ingest only if source files changed.

    Cheap scan when nothing has changed: just a directory walk + stat calls,
    no embedding model invocation, no chromadb queries.
    """
    global _last_ingest_mtime, _last_ingest_time

    try:
        newest = await asyncio.to_thread(_newest_source_mtime)
        if newest <= 0:
            return  # source dir missing or empty

        if newest <= _last_ingest_mtime:
            return  # no changes since last successful run

        human = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest))
        logger.info("[RAG_INGEST] Source changes detected (newest mtime: %s), running ingest", human)

        t0 = time.time()
        stats = await asyncio.to_thread(_run_ingest_sync)
        elapsed = time.time() - t0

        if stats["changed_files"] > 0 or stats["new_chunks"] > 0:
            logger.info(
                "[RAG_INGEST] Complete in %.1fs: %d files changed, %d chunks deleted, %d new, %d total",
                elapsed,
                stats["changed_files"],
                stats["deleted_chunks"],
                stats["new_chunks"],
                stats["total"],
            )
        else:
            logger.debug("[RAG_INGEST] Scan complete in %.1fs, no chunk changes", elapsed)

        _last_ingest_mtime = newest
        _last_ingest_time = time.time()
    except Exception as e:
        logger.error("[RAG_INGEST] Ingest failed: %s", e, exc_info=True)
