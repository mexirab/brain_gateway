import argparse
import textwrap
import requests
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

SYSTEM_RULES = """\
You are an assistant with access to private notes (CONTEXT).
RULES:
- Use ONLY the provided CONTEXT to answer.
- If CONTEXT is insufficient, say: "I don't know based on my saved notes."
- Do not invent facts or fill gaps.
- Keep the answer concise and practical.
- End with a short Sources list (file :: section).
"""

def cosine_from_l2(l2: float) -> float:
    # For normalized embeddings: cosine ≈ 1 - (L2^2 / 2)
    return 1.0 - (l2 * l2) / 2.0

def build_context(docs, metas, dists, max_chars: int = 12000):
    blocks = []
    sources = []
    used = 0

    for doc, meta, dist in zip(docs, metas, dists):
        fp = meta.get("file_path") or "unknown"
        sec = meta.get("section") or "unknown"
        cos = cosine_from_l2(dist)

        src_line = f"{fp} :: {sec} (cos≈{cos:.3f})"
        block = f"[SOURCE]\n{src_line}\n[TEXT]\n{doc.strip()}\n"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
        sources.append((fp, sec, cos))

    return "\n---\n".join(blocks), sources

def llm_call_chat(base_url: str, model: str, system: str, user: str, temperature: float = 0.2) -> str:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "stream": False,
    }
    r = requests.post(url, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

def llm_call_completion(base_url: str, model: str, prompt: str, temperature: float = 0.2) -> str:
    url = base_url.rstrip("/") + "/v1/completions"
    payload = {
        "model": model,
        "prompt": prompt,
        "temperature": temperature,
        "max_tokens": 600,
        "stream": False,
    }
    r = requests.post(url, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["text"] or "").strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--q", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--min-cos", type=float, default=0.30)
    ap.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--llm-base-url", default="http://helios:8080")
    ap.add_argument("--llm-model", default="")
    ap.add_argument("--temperature", type=float, default=0.2)
    args = ap.parse_args()

    # --- Discover model if not provided ---
    if not args.llm_model.strip():
        m = requests.get(args.llm_base_url.rstrip("/") + "/v1/models", timeout=30).json()
        # Prefer "data" list if present
        if "data" in m and m["data"]:
            args.llm_model = m["data"][0]["id"]
        elif "models" in m and m["models"]:
            args.llm_model = m["models"][0]["name"]
        else:
            raise SystemExit("Could not discover model id from /v1/models. Pass --llm-model explicitly.")

    # --- Chroma retrieval ---
    client = chromadb.PersistentClient(path=args.persist, settings=Settings(anonymized_telemetry=False))
    coll = client.get_collection(args.collection)

    embedder = SentenceTransformer(args.embed_model)
    q_emb = embedder.encode([args.q], normalize_embeddings=True).tolist()[0]

    res = coll.query(
        query_embeddings=[q_emb],
        n_results=args.k,
        where={"kind": "chunk"},
        include=["documents", "metadatas", "distances"],
    )

    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    if not docs:
        print("No results returned from memory index.")
        return

    best_cos = cosine_from_l2(dists[0])
    if best_cos < args.min_cos:
        print(f"Not enough relevant memory to answer (best cosine≈{best_cos:.4f} < {args.min_cos:.2f}).")
        return

    context_text, sources = build_context(docs, metas, dists)

    user_prompt = textwrap.dedent(f"""\
    QUESTION:
    {args.q}

    CONTEXT:
    {context_text}

    INSTRUCTIONS:
    - Answer ONLY using CONTEXT.
    - If CONTEXT is insufficient: say "I don't know based on my saved notes."
    - Include a short Sources list (file :: section) at the end.
    """)

    # Try chat endpoint first; fallback to completions if not supported
    try:
        answer = llm_call_chat(args.llm_base_url, args.llm_model, SYSTEM_RULES, user_prompt, args.temperature)
    except requests.HTTPError as e:
        # Some llama.cpp builds only expose /v1/completions
        prompt = f"{SYSTEM_RULES}\n\n{user_prompt}\n\nANSWER:\n"
        answer = llm_call_completion(args.llm_base_url, args.llm_model, prompt, args.temperature)

    # If model forgot sources, we append them anyway (still useful for provenance)
    if "Sources:" not in answer:
        answer = answer.rstrip() + "\n\nSources:"

    print(answer.rstrip())
    # Add sources (dedup)
    seen = set()
    for fp, sec, cos in sources:
        key = (fp, sec)
        if key in seen:
            continue
        seen.add(key)
        print(f"- {fp} :: {sec} (cos≈{cos:.3f})")

if __name__ == "__main__":
    main()
