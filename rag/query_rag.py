import argparse
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--q", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--min-cos", type=float, default=0.30, help="Minimum cosine similarity to accept results")
    args = ap.parse_args()

    client = chromadb.PersistentClient(
        path=args.persist,
        settings=Settings(anonymized_telemetry=False),
    )
    coll = client.get_collection(args.collection)

    model = SentenceTransformer(args.embed_model)
    q_embedding = model.encode([args.q], normalize_embeddings=True).tolist()[0]

    res = coll.query(
        query_embeddings=[q_embedding],
        n_results=args.k,
        where={"kind": "chunk"},  # filter out FILE MARKERS
        include=["documents", "metadatas", "distances"],
    )

    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    if not docs:
        print("No results returned. (Either collection is empty, or filter didn’t match.)")
        return

    # Relevance gate: compute cosine for best hit
    best_l2 = dists[0]
    best_cos = 1.0 - (best_l2 * best_l2) / 2.0
    if best_cos < args.min_cos:
        print(f"Not enough relevant memory to answer (best cosine≈{best_cos:.4f} < {args.min_cos:.2f}).")
        return

    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), start=1):
        # For normalized embeddings: cosine ≈ 1 - (L2^2 / 2)
        cos = 1.0 - (dist * dist) / 2.0
        print("=" * 90)
        print(f"{i}) l2={dist:.4f}  cosine≈{cos:.4f}")
        print(f"   file={meta.get('file_path')}")
        print(f"   section={meta.get('section')}")
        print()
        print((doc or "")[:900].strip())

if __name__ == "__main__":
    main()
