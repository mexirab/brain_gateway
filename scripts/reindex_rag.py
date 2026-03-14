#!/usr/bin/env python3
"""
Re-index RAG embeddings with a new embedding model.

One-time migration script that reads all existing ChromaDB documents,
re-embeds them with the specified model, and upserts vectors in-place
(same doc IDs, same metadata). Runs test queries to verify quality.

Usage:
    python scripts/reindex_rag.py \
        --persist ~/.local/share/chroma/personal_rag \
        --collection nadim_rag \
        --embed-model nomic-ai/nomic-embed-text-v2-moe

    # Dry run (show stats without modifying):
    python scripts/reindex_rag.py \
        --persist ~/.local/share/chroma/personal_rag \
        --collection nadim_rag \
        --dry-run
"""

import argparse
import sys
import time
from pathlib import Path

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# Test queries to verify re-indexing quality
TEST_QUERIES = [
    "what medications do I take in the morning",
    "what are my current projects",
    "what is my favorite restaurant",
]

BATCH_SIZE = 256


def get_all_docs(collection, batch_size: int = 5000):
    """Retrieve all documents from a ChromaDB collection."""
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0

    while True:
        result = collection.get(
            include=["documents", "metadatas"],
            limit=batch_size,
            offset=offset,
        )
        ids = result.get("ids", [])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])

        if not ids:
            break

        all_ids.extend(ids)
        all_docs.extend(docs)
        all_metas.extend(metas)
        offset += len(ids)

    return all_ids, all_docs, all_metas


def run_test_queries(collection, model, queries):
    """Run test queries and return results with similarity scores."""
    results = []
    for query in queries:
        embedding = model.encode([query], normalize_embeddings=True).tolist()
        res = collection.query(
            query_embeddings=embedding,
            n_results=3,
            include=["documents", "distances", "metadatas"],
        )

        query_results = []
        for i, doc_id in enumerate(res["ids"][0]):
            distance = res["distances"][0][i]
            doc = res["documents"][0][i][:120] if res["documents"][0][i] else ""
            meta = res["metadatas"][0][i] if res["metadatas"][0] else {}
            query_results.append(
                {
                    "id": doc_id,
                    "distance": distance,
                    "file_path": meta.get("file_path", ""),
                    "preview": doc,
                }
            )
        results.append({"query": query, "results": query_results})
    return results


def main():
    ap = argparse.ArgumentParser(description="Re-index RAG embeddings with a new model")
    ap.add_argument(
        "--persist",
        required=True,
        help="ChromaDB persistence directory",
    )
    ap.add_argument(
        "--collection",
        required=True,
        help="ChromaDB collection name",
    )
    ap.add_argument(
        "--embed-model",
        default="nomic-ai/nomic-embed-text-v2-moe",
        help="New embedding model (default: nomic-ai/nomic-embed-text-v2-moe)",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Batch size for re-embedding (default: {BATCH_SIZE})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show stats and test queries without modifying data",
    )
    args = ap.parse_args()

    persist = Path(args.persist).expanduser().resolve()
    if not persist.exists():
        print(f"Error: persist directory does not exist: {persist}")
        sys.exit(1)

    print(f"ChromaDB path: {persist}")
    print(f"Collection:    {args.collection}")
    print(f"Embed model:   {args.embed_model}")
    print(f"Batch size:    {args.batch_size}")
    print(f"Dry run:       {args.dry_run}")
    print()

    # Connect to ChromaDB
    client = chromadb.PersistentClient(
        path=str(persist),
        settings=Settings(anonymized_telemetry=False),
    )

    try:
        collection = client.get_collection(name=args.collection)
    except Exception:
        print(f"Error: collection '{args.collection}' not found")
        sys.exit(1)

    total_count = collection.count()
    print(f"Total documents in collection: {total_count}")

    if total_count == 0:
        print("Nothing to re-index.")
        return

    # Load all documents
    print("Loading all documents...")
    all_ids, all_docs, all_metas = get_all_docs(collection)
    print(f"Loaded {len(all_ids)} documents")

    # Count by kind
    kind_counts = {}
    for meta in all_metas:
        kind = (meta or {}).get("kind", "unknown")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    print("\nDocument breakdown:")
    for kind, count in sorted(kind_counts.items()):
        print(f"  {kind}: {count}")

    # Load embedding model
    print(f"\nLoading embedding model: {args.embed_model}")
    t0 = time.time()
    model = SentenceTransformer(args.embed_model)
    print(f"Model loaded in {time.time() - t0:.1f}s")
    print(f"Embedding dimension: {model.get_sentence_embedding_dimension()}")

    if args.dry_run:
        # Run test queries with CURRENT embeddings, then with new model
        print("\n--- Test queries (current embeddings) ---")
        current_results = run_test_queries(collection, model, TEST_QUERIES)
        for qr in current_results:
            print(f"\nQuery: {qr['query']}")
            for r in qr["results"]:
                print(f"  [{r['distance']:.4f}] {r['file_path']}: {r['preview'][:80]}...")
        print("\nDry run complete. No changes made.")
        return

    # Re-embed and upsert in batches
    print(f"\nRe-embedding {len(all_ids)} documents...")
    t0 = time.time()
    batches_done = 0

    for i in tqdm(range(0, len(all_ids), args.batch_size), desc="Re-indexing"):
        batch_ids = all_ids[i : i + args.batch_size]
        batch_docs = all_docs[i : i + args.batch_size]
        batch_metas = all_metas[i : i + args.batch_size]

        # Filter out None docs (shouldn't happen, but defensive)
        valid = [(id_, doc, meta) for id_, doc, meta in zip(batch_ids, batch_docs, batch_metas) if doc]
        if not valid:
            continue

        v_ids, v_docs, v_metas = zip(*valid)

        # Generate new embeddings
        embeddings = model.encode(list(v_docs), normalize_embeddings=True).tolist()

        # Upsert with new embeddings (same IDs, same docs, same metadata)
        collection.upsert(
            ids=list(v_ids),
            documents=list(v_docs),
            metadatas=list(v_metas),
            embeddings=embeddings,
        )
        batches_done += 1

    elapsed = time.time() - t0
    print(f"\nRe-indexing complete in {elapsed:.1f}s ({batches_done} batches)")
    print(f"Total documents: {collection.count()}")

    # Run test queries with new embeddings
    print("\n--- Test queries (new embeddings) ---")
    test_results = run_test_queries(collection, model, TEST_QUERIES)
    for qr in test_results:
        print(f"\nQuery: {qr['query']}")
        for r in qr["results"]:
            print(f"  [{r['distance']:.4f}] {r['file_path']}: {r['preview'][:80]}...")

    print("\nDone! Verify results above look reasonable before deploying.")


if __name__ == "__main__":
    main()
