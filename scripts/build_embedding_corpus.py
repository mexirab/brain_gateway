"""Build positive-pair training corpus for personal embedding fine-tune.

Sources:
  1. RAG markdown corpus       (~/rag/nadim_rag/**/*.md|txt)
  2. mempalace ChromaDB        (optional; needs chromadb + CHROMA_PERSIST)
  3. Open WebUI chat history   (webui.db → chat.chat JSON)
  4. state_store chat_messages (brain_state.db)
  5. Claude Code session jsonl (~/.claude/projects/-opt-helios-gateway-mvp/*.jsonl)
  6. Claude Code memory files  (~/.claude/projects/.../memory/*.md)

Produces a single pairs.jsonl with fields:
  {"anchor": str, "positive": str, "strategy": str, "domain": str}

Usage (run from repo root):
  python3 scripts/build_embedding_corpus.py \\
      --rag-dir ~/rag/nadim_rag \\
      --webui-db /tmp/webui.db \\
      --state-db data/app/brain_state.db \\
      --cc-dir ~/.claude/projects/-opt-helios-gateway-mvp \\
      --out data/embedding_finetune/pairs_v1.jsonl

Any source can be skipped by passing an empty / nonexistent path.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

MIN_LEN = 15
MAX_LEN = 2000
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
PAIRS_PER_SESSION_CAP = 60
DOMAIN_CAP_RATIO = 0.60

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)api[_-]?key['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

# Fernet ciphertext blobs from auto_learn start with `gAAAA` and run 60+ chars.
# Any chunk where >25% of its characters live inside such a blob is junk.
FERNET_PATTERN = re.compile(r"gAAAAA[A-Za-z0-9_\-=]{40,}")
# Files to skip wholesale — directories that store only encrypted auto-learned
# facts (useless for a word-level embedding model).
RAG_EXCLUDE_DIRS = {"60_learned", "90_archive"}


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _looks_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def _accept(text: str) -> bool:
    if not text:
        return False
    if len(text) < MIN_LEN:
        return False
    if _looks_secret(text):
        return False
    # Drop anything dominated by Fernet ciphertext blobs
    ct_chars = sum(len(m.group(0)) for m in FERNET_PATTERN.finditer(text))
    if ct_chars > 0.25 * len(text):
        return False
    return True


def _truncate(text: str) -> str:
    return text[:MAX_LEN] if len(text) > MAX_LEN else text


def _fingerprint(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _five_grams(text: str) -> set[str]:
    tokens = re.findall(r"\w+", text.lower())
    return {" ".join(tokens[i : i + 5]) for i in range(len(tokens) - 4)} if len(tokens) >= 5 else set()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Greedy paragraph-aware chunker."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            if len(para) > size:
                for i in range(0, len(para), size - overlap):
                    chunks.append(para[i : i + size])
                buf = ""
            else:
                buf = para
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= MIN_LEN]


def split_markdown_sections(text: str) -> list[str]:
    """Split on H1/H2 headers; return non-empty section bodies (header stripped)."""
    parts = re.split(r"(?m)^#{1,2}\s+.*$", text)
    return [p.strip() for p in parts if len(p.strip()) >= MIN_LEN]


# ---------------------------------------------------------------------------
# Source extractors
# ---------------------------------------------------------------------------


def extract_rag_files(root: Path) -> list[dict]:
    """One item per source file. Keeps full text for chunking downstream."""
    items: list[dict] = []
    if not root.exists():
        return items
    for fp in sorted(root.rglob("*")):
        if fp.suffix.lower() not in {".md", ".txt"}:
            continue
        if any(part in RAG_EXCLUDE_DIRS for part in fp.relative_to(root).parts):
            continue
        try:
            raw = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _clean(raw)
        if not _accept(text):
            continue
        items.append({
            "id": f"rag:{fp.relative_to(root)}",
            "text": text,
            "domain": "personal",
            "source": "rag_file",
            "session_id": str(fp.relative_to(root)),
        })
    return items


def extract_cc_memory(root: Path) -> list[dict]:
    items: list[dict] = []
    mem_dir = root / "memory"
    if not mem_dir.exists():
        return items
    for fp in sorted(mem_dir.glob("*.md")):
        try:
            raw = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Strip frontmatter
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) == 3:
                raw = parts[2]
        text = _clean(raw)
        if not _accept(text):
            continue
        items.append({
            "id": f"cc_mem:{fp.name}",
            "text": text,
            "domain": "personal",
            "source": "cc_memory",
            "session_id": fp.name,
        })
    return items


def extract_webui(db_path: Path) -> dict[str, list[str]]:
    """Return {conversation_id: [user_turn_text, ...]} for OWUI history."""
    sessions: dict[str, list[str]] = {}
    if not db_path.exists():
        return sessions
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id, chat FROM chat").fetchall()
    finally:
        conn.close()
    for chat_id, blob in rows:
        try:
            doc = json.loads(blob)
        except (TypeError, json.JSONDecodeError):
            continue
        messages = doc.get("messages") or []
        if not messages and isinstance(doc.get("history"), dict):
            messages = list(doc["history"].get("messages", {}).values())
        turns: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if not isinstance(content, str):
                continue
            text = _clean(content)
            if _accept(text):
                turns.append(_truncate(text))
        if turns:
            sessions[f"owui:{chat_id}"] = turns
    return sessions


def extract_state_store(db_path: Path) -> dict[str, list[tuple[str, str]]]:
    """Return {conversation_id: [(role, text), ...]} from brain_state."""
    sessions: dict[str, list[tuple[str, str]]] = {}
    if not db_path.exists():
        return sessions
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT conversation_id, role, content, created_at FROM chat_messages "
            "ORDER BY conversation_id, created_at"
        ).fetchall()
    finally:
        conn.close()
    for conv_id, role, content, _ in rows:
        if role not in {"user", "assistant"}:
            continue
        text = _clean(content or "")
        if not _accept(text):
            continue
        sessions.setdefault(f"ss:{conv_id}", []).append((role, _truncate(text)))
    return sessions


def _cc_extract_user_content(msg: dict) -> str | None:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str):
                    texts.append(t)
            elif isinstance(part, dict) and part.get("type") == "tool_result":
                # Skip tool results entirely
                return None
        return "\n".join(texts) if texts else None
    return None


def extract_cc_sessions(root: Path) -> dict[str, list[str]]:
    """Return {session_id: [user_text, ...]} from Claude Code jsonl files."""
    sessions: dict[str, list[str]] = {}
    if not root.exists():
        return sessions
    for fp in sorted(root.glob("*.jsonl")):
        turns: list[str] = []
        try:
            with fp.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "user":
                        continue
                    msg = rec.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    text = _cc_extract_user_content(msg)
                    if not text:
                        continue
                    text = _clean(text)
                    # Drop short CLI interactions, tool artifacts, system tags
                    if text.startswith("<") or text.startswith("["):
                        continue
                    if not _accept(text):
                        continue
                    turns.append(_truncate(text))
        except OSError:
            continue
        if turns:
            sessions[f"cc:{fp.stem}"] = turns
    return sessions


def extract_mempalace() -> list[dict]:
    """Optional — requires chromadb + CHROMA_PERSIST env var pointing at a live collection."""
    chroma_path = os.environ.get("CHROMA_PERSIST")
    collection_name = os.environ.get("PALACE_COLLECTION", "mempalace")
    if not chroma_path or not Path(chroma_path).exists():
        return []
    try:
        import chromadb  # type: ignore
        from chromadb.config import Settings as ChromaSettings  # type: ignore
    except ImportError:
        print("[mempalace] chromadb not installed — skipping", file=sys.stderr)
        return []
    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    try:
        col = client.get_collection(collection_name)
    except Exception as e:  # noqa: BLE001
        print(f"[mempalace] collection {collection_name!r} unavailable: {e}", file=sys.stderr)
        return []
    data = col.get(include=["documents", "metadatas"])
    items: list[dict] = []
    for idx, (doc, meta) in enumerate(zip(data.get("documents", []), data.get("metadatas", []))):
        if not isinstance(doc, str):
            continue
        text = _clean(doc)
        if not _accept(text):
            continue
        wing = (meta or {}).get("wing", "unknown")
        items.append({
            "id": f"palace:{idx}",
            "text": _truncate(text),
            "domain": "personal",
            "source": "mempalace",
            "session_id": f"palace_wing:{wing}",
        })
    return items


# ---------------------------------------------------------------------------
# Pair generators
# ---------------------------------------------------------------------------


def pairs_from_doc(text: str, domain: str) -> Iterable[tuple[str, str, str]]:
    """Same-document chunk pairs + adjacent-section pairs."""
    # Strategy 1: same-document chunks
    chunks = chunk_text(text)
    if len(chunks) >= 2:
        for a, b in itertools.combinations(chunks, 2):
            yield (a, b, "same_doc")

    # Strategy 2: adjacent markdown sections
    sections = split_markdown_sections(text)
    for a, b in zip(sections, sections[1:]):
        yield (a, b, "adj_section")


def pairs_from_session_turns(turns: list[str]) -> Iterable[tuple[str, str, str]]:
    """Adjacent user turns within the same session."""
    for a, b in zip(turns, turns[1:]):
        if a != b:
            yield (a, b, "adj_turn")


def pairs_from_qa(msgs: list[tuple[str, str]]) -> Iterable[tuple[str, str, str]]:
    """Query↔answer pairs from an interleaved (role, text) sequence."""
    for (r1, t1), (r2, t2) in zip(msgs, msgs[1:]):
        if r1 == "user" and r2 == "assistant":
            yield (t1, t2, "qa")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rag-dir", type=Path, default=Path.home() / "rag" / "nadim_rag")
    ap.add_argument("--webui-db", type=Path, default=Path("/tmp/webui.db"))
    ap.add_argument("--state-db", type=Path, default=Path("data/app/brain_state.db"))
    ap.add_argument("--cc-dir", type=Path, default=Path.home() / ".claude" / "projects" / "-opt-helios-gateway-mvp")
    ap.add_argument("--out", type=Path, default=Path("data/embedding_finetune/pairs_v1.jsonl"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sample-preview", type=int, default=20)
    ap.add_argument("--skip-mempalace", action="store_true")
    args = ap.parse_args()

    random.seed(args.seed)

    # --- Extract ---
    print("Extracting sources…", file=sys.stderr)
    rag_docs = extract_rag_files(args.rag_dir)
    cc_mem = extract_cc_memory(args.cc_dir)
    owui_sessions = extract_webui(args.webui_db)
    state_sessions = extract_state_store(args.state_db)
    cc_sessions = extract_cc_sessions(args.cc_dir)
    palace_items = [] if args.skip_mempalace else extract_mempalace()

    print(f"  rag files         : {len(rag_docs)}", file=sys.stderr)
    print(f"  cc memory files   : {len(cc_mem)}", file=sys.stderr)
    print(f"  owui sessions     : {len(owui_sessions)} "
          f"({sum(len(v) for v in owui_sessions.values())} turns)", file=sys.stderr)
    print(f"  state sessions    : {len(state_sessions)} "
          f"({sum(len(v) for v in state_sessions.values())} msgs)", file=sys.stderr)
    print(f"  cc sessions       : {len(cc_sessions)} "
          f"({sum(len(v) for v in cc_sessions.values())} turns)", file=sys.stderr)
    print(f"  mempalace chunks  : {len(palace_items)}", file=sys.stderr)

    # --- Generate pairs ---
    pairs: list[dict] = []

    # Strategy 1+2: same-doc / adj-section over prose sources (personal domain)
    for doc in rag_docs + cc_mem:
        for a, p, strat in pairs_from_doc(doc["text"], doc["domain"]):
            pairs.append({"anchor": a, "positive": p, "strategy": strat, "domain": doc["domain"]})

    # Mempalace chunks are small — cluster by wing, treat same-wing chunks as
    # weak positives. Cap per-wing to avoid blowup.
    if palace_items:
        by_wing: dict[str, list[str]] = defaultdict(list)
        for item in palace_items:
            by_wing[item["session_id"]].append(item["text"])
        for wing, texts in by_wing.items():
            combos = list(itertools.combinations(texts, 2))
            random.shuffle(combos)
            for a, p in combos[:PAIRS_PER_SESSION_CAP]:
                pairs.append({"anchor": a, "positive": p, "strategy": "palace_wing", "domain": "personal"})

    # Strategy 3: adjacent user turns — personal (owui) + technical (cc)
    def _cap(session_pairs: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        random.shuffle(session_pairs)
        return session_pairs[:PAIRS_PER_SESSION_CAP]

    for sid, turns in owui_sessions.items():
        sp = list(pairs_from_session_turns(turns))
        for a, p, s in _cap(sp):
            pairs.append({"anchor": a, "positive": p, "strategy": s, "domain": "personal"})

    for sid, turns in cc_sessions.items():
        sp = list(pairs_from_session_turns(turns))
        for a, p, s in _cap(sp):
            pairs.append({"anchor": a, "positive": p, "strategy": s, "domain": "technical"})

    # Strategy 4: Q↔A pairs — only from brain_state (has interleaved roles).
    for sid, msgs in state_sessions.items():
        sp = list(pairs_from_qa(msgs))
        for a, p, s in _cap(sp):
            pairs.append({"anchor": a, "positive": p, "strategy": s, "domain": "personal"})

    print(f"raw pairs         : {len(pairs)}", file=sys.stderr)

    # --- Dedupe ---
    seen_keys: set[tuple[str, str]] = set()
    fingerprints: dict[str, set[str]] = defaultdict(set)  # strategy → fp set
    deduped: list[dict] = []
    near_dupes = 0
    for row in pairs:
        if row["anchor"] == row["positive"]:
            continue
        k = (_fingerprint(row["anchor"]), _fingerprint(row["positive"]))
        if k in seen_keys or (k[1], k[0]) in seen_keys:
            continue
        seen_keys.add(k)
        # Near-dupe check — drop if anchor/positive 5-gram jaccard > 0.9
        if _jaccard(_five_grams(row["anchor"]), _five_grams(row["positive"])) > 0.9:
            near_dupes += 1
            continue
        deduped.append(row)
    print(f"after dedupe      : {len(deduped)} (dropped {near_dupes} near-dupes)", file=sys.stderr)

    # --- Domain balance cap ---
    # Enforce: no domain > DOMAIN_CAP_RATIO of the *final* total. We solve for
    # the largest total achievable given the smallest-domain count:
    #     largest_allowed = min_domain_count / (1 - DOMAIN_CAP_RATIO)
    # then cap every other domain at DOMAIN_CAP_RATIO * largest_allowed.
    per_domain: Counter[str] = Counter(p["domain"] for p in deduped)
    if len(per_domain) >= 2:
        min_count = min(per_domain.values())
        largest_allowed_total = int(min_count / (1 - DOMAIN_CAP_RATIO))
        per_domain_cap = int(largest_allowed_total * DOMAIN_CAP_RATIO)
        limits: dict[str, int] = {d: min(n, per_domain_cap) for d, n in per_domain.items()}
    else:
        limits = dict(per_domain)
    counts: Counter[str] = Counter()
    random.shuffle(deduped)
    capped: list[dict] = []
    for row in deduped:
        d = row["domain"]
        if counts[d] < limits[d]:
            capped.append(row)
            counts[d] += 1

    # --- Report ---
    print("=" * 60, file=sys.stderr)
    print(f"final pairs       : {len(capped)}", file=sys.stderr)
    print("by domain         :", file=sys.stderr)
    for d, n in Counter(p["domain"] for p in capped).most_common():
        pct = 100.0 * n / max(len(capped), 1)
        print(f"  {d:12s} {n:6d}  ({pct:5.1f}%)", file=sys.stderr)
    print("by strategy       :", file=sys.stderr)
    for s, n in Counter(p["strategy"] for p in capped).most_common():
        pct = 100.0 * n / max(len(capped), 1)
        print(f"  {s:14s} {n:6d}  ({pct:5.1f}%)", file=sys.stderr)
    lens = [len(p["anchor"]) + len(p["positive"]) for p in capped]
    if lens:
        lens_sorted = sorted(lens)
        print("length histogram  :", file=sys.stderr)
        print(f"  min {lens_sorted[0]}  p25 {lens_sorted[len(lens)//4]}  "
              f"median {lens_sorted[len(lens)//2]}  p75 {lens_sorted[3*len(lens)//4]}  "
              f"max {lens_sorted[-1]}", file=sys.stderr)

    # --- Write output ---
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in capped:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote             : {args.out}", file=sys.stderr)

    # --- Sample preview ---
    print("=" * 60, file=sys.stderr)
    print(f"SAMPLE ({args.sample_preview} pairs) — visually confirm these should be 'close':",
          file=sys.stderr)
    sample = random.sample(capped, min(args.sample_preview, len(capped)))
    for i, row in enumerate(sample, 1):
        print(f"\n[{i}] strategy={row['strategy']} domain={row['domain']}", file=sys.stderr)
        print(f"  A: {row['anchor'][:140]!r}", file=sys.stderr)
        print(f"  B: {row['positive'][:140]!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
