import argparse, hashlib, re
from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Any

import chromadb
from chromadb.config import Settings
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

TEXT_EXTS = {".md", ".txt", ".log", ".rst"}
IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)\s*$", re.MULTILINE)

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()

def iter_text_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_dir() and p.name in IGNORE_DIRS:
            continue
        if p.is_file() and p.suffix.lower() in TEXT_EXTS:
            yield p

def split_markdown_by_headers(text: str) -> List[Tuple[str, str]]:
    """Split markdown by headers, including parent header context in each chunk."""
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return [("document", text)]

    # Track parent headers at each level
    parent_headers = {}  # level -> header text

    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        lvl = len(m.group(1))
        title = m.group(2).strip()
        body = text[m.end():end].strip()
        header_line = text[m.start():m.end()].strip()

        # Update parent headers: current level and clear deeper levels
        parent_headers[lvl] = header_line
        for l in list(parent_headers.keys()):
            if l > lvl:
                del parent_headers[l]

        # Build context: include all parent headers
        context_parts = []
        for l in sorted(parent_headers.keys()):
            if l < lvl:  # Only include parents, not self
                context_parts.append(parent_headers[l])

        # Prepend parent context to chunk
        if context_parts:
            context_prefix = "\n".join(context_parts) + "\n\n"
            chunk_content = f"{context_prefix}{header_line}\n\n{body}".strip()
        else:
            chunk_content = f"{header_line}\n\n{body}".strip()

        sections.append((f"h{lvl}:{title}", chunk_content))
    return sections

def chunk_text(text: str, target_chars: int = 2400, overlap_chars: int = 300) -> List[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks, i, n = [], 0, len(text)
    while i < n:
        j = min(i + target_chars, n)
        cut = text.rfind("\n\n", i, j)
        if cut != -1 and cut > i + target_chars * 0.6:
            j = cut
        chunk = text[i:j].strip()
        if chunk:
            chunks.append(chunk)
        if j >= n:
            break
        i = max(0, j - overlap_chars)
    return chunks

def delete_by_file_path(coll, file_path: str, batch_size: int = 5000) -> int:
    """
    Chroma can't delete by prefix, but we can query IDs by metadata filter.
    """
    deleted = 0
    offset = 0
    while True:
        res = coll.get(where={"file_path": file_path}, include=["metadatas"], limit=batch_size, offset=offset)
        ids = res.get("ids", [])
        if not ids:
            break
        coll.delete(ids=ids)
        deleted += len(ids)
        # after delete, restart from 0 (offset becomes invalid)
        offset = 0
    return deleted

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--persist", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--embed-model", default="nomic-ai/nomic-embed-text-v2-moe")
    ap.add_argument("--target-chars", type=int, default=2400)
    ap.add_argument("--overlap-chars", type=int, default=300)
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    persist = Path(args.persist).expanduser().resolve()
    persist.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(
        path=str(persist),
        settings=Settings(anonymized_telemetry=False),
    )
    coll = client.get_or_create_collection(name=args.collection)
    model = SentenceTransformer(args.embed_model, trust_remote_code=True)

    # Map existing file markers: file::<rel> -> hash
    existing_hash_by_rel: Dict[str, str] = {}
    offset = 0
    while True:
        res = coll.get(where={"kind": "file_marker"}, include=["metadatas"], limit=5000, offset=offset)
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        if not ids:
            break
        for _id, meta in zip(ids, metas):
            if _id.startswith("file::") and meta and "file_path" in meta:
                existing_hash_by_rel[meta["file_path"]] = meta.get("file_hash", "")
        offset += len(ids)

    files = list(iter_text_files(source))
    seen = set()

    # We upsert everything in batches
    upsert_ids: List[str] = []
    upsert_docs: List[str] = []
    upsert_metas: List[Dict[str, Any]] = []

    # Track changes
    changed_files = 0
    deleted_chunks_total = 0

    for f in tqdm(files, desc="Indexing"):
        rel = str(f.relative_to(source))
        seen.add(rel)

        raw = f.read_text(encoding="utf-8", errors="replace")
        h = sha256_text(raw)

        old_hash = existing_hash_by_rel.get(rel)
        if old_hash == h:
            continue  # unchanged

        # file changed or new: delete all old chunks + marker for this file_path
        deleted_chunks_total += delete_by_file_path(coll, rel)
        # also delete its marker id specifically (safe if missing)
        coll.delete(ids=[f"file::{rel}"])

        changed_files += 1

        sections = split_markdown_by_headers(raw) if f.suffix.lower() == ".md" else [("document", raw)]
        chunks, chunk_metas = [], []
        for sec_title, sec_text in sections:
            for k, ch in enumerate(chunk_text(sec_text, args.target_chars, args.overlap_chars)):
                chunks.append(ch)
                chunk_metas.append({
                    "file_path": rel,
                    "file_hash": h,
                    "section": sec_title,
                    "chunk_index": k,
                    "source_root": str(source),
                    "kind": "chunk",
                })

        if not chunks:
            continue

        # Deterministic IDs per file hash (avoid collisions across edits)
        filehash_prefix = h[:12]
        chunk_ids = [f"chunk::{rel}::{m['section']}::{m['chunk_index']}::{filehash_prefix}" for m in chunk_metas]

        upsert_ids.extend(chunk_ids)
        upsert_docs.extend(chunks)
        upsert_metas.extend(chunk_metas)

        # Marker doc (NO list metadata)
        marker_id = f"file::{rel}"
        marker_doc = f"FILE MARKER\npath={rel}\nhash={h}\nchunks={len(chunk_ids)}"
        marker_meta = {
            "file_path": rel,
            "file_hash": h,
            "kind": "file_marker",
            "chunk_count": len(chunk_ids),
        }
        upsert_ids.append(marker_id)
        upsert_docs.append(marker_doc)
        upsert_metas.append(marker_meta)

    # Handle removed files: markers exist but file no longer present
    removed = set(existing_hash_by_rel.keys()) - seen
    for rel in removed:
        deleted_chunks_total += delete_by_file_path(coll, rel)
        coll.delete(ids=[f"file::{rel}"])

    # Upsert in batches
    for i in range(0, len(upsert_ids), 256):
        b_ids = upsert_ids[i:i+256]
        b_docs = upsert_docs[i:i+256]
        b_metas = upsert_metas[i:i+256]
        embs = model.encode(b_docs, normalize_embeddings=True).tolist()
        coll.upsert(ids=b_ids, documents=b_docs, metadatas=b_metas, embeddings=embs)

    print("Done.")
    print(f"Source: {source}")
    print(f"Persist: {persist}")
    print(f"Collection: {args.collection}")
    print(f"Changed files: {changed_files}")
    print(f"Deleted old records: {deleted_chunks_total}")
    print(f"Total records: {coll.count()}")

if __name__ == "__main__":
    main()
