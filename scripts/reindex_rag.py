#!/usr/bin/env python3
"""
Re-index RAG embeddings with a new embedding model.

Reads all documents from a ChromaDB collection, re-embeds them with the
specified model, and upserts vectors in-place (same doc IDs, same metadata).
Runs test queries to verify quality.

Supports --add-palace-metadata to route existing docs into palace wings/rooms
based on their content and source metadata. Use this when migrating from the
old personal_rag collection to the unified mempalace collection.

Usage:
    python scripts/reindex_rag.py \
        --persist /chroma/personal_rag \
        --collection mempalace \
        --embed-model nomic-ai/nomic-embed-text-v2-moe

    # Migrate from old collection to palace with wing/room routing:
    python scripts/reindex_rag.py \
        --persist /chroma/personal_rag \
        --collection personal_rag \
        --target-collection mempalace \
        --add-palace-metadata \
        --palace-yaml /app/config/palace.yaml

    # Dry run (show stats without modifying):
    python scripts/reindex_rag.py \
        --persist /chroma/personal_rag \
        --collection mempalace \
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


def _route_to_wing_room(doc_text, meta, routing_rules=None):
    """Route a document to a palace wing/room based on content and metadata."""
    import re as _re

    # Already has palace metadata — keep it
    if meta and meta.get("wing"):
        return meta.get("wing", ""), meta.get("room", "")

    text_lower = (doc_text or "")[:500].lower()
    source = (meta or {}).get("source", "")
    category = (meta or {}).get("category", "")

    # Source-based routing
    if source == "auto_learn":
        # Try regex routing rules if available
        if routing_rules:
            for rule in routing_rules:
                compiled = rule.get("_compiled")
                if compiled and compiled.search(text_lower):
                    return rule.get("wing", "personal"), rule.get("room", "")

        # Category-based fallback for auto-learn. Must cover every category
        # the auto_learn extraction prompt emits, otherwise migrated facts
        # silently land in ("personal", "") (unroomed) and become harder to
        # find via wing/room filtering.
        cat_map = {
            "health": ("personal", "health"),
            "medication": ("personal", "health"),
            "routine": ("personal", "routines"),
            "preference": ("jess", "preferences"),
            "identity": ("personal", ""),
            "work": ("brain_gateway", ""),
            "technical": ("brain_gateway", ""),
            "pattern": ("personal", ""),
            "relationship": ("personal", "relationships"),
            "goal": ("personal", "goals"),
            "emotion": ("personal", ""),
        }
        if category in cat_map:
            return cat_map[category]
        return "personal", ""

    if source == "document_vault":
        vault_map = {
            "financial": ("personal", "finance"),
            "medical": ("personal", "health"),
        }
        if category in vault_map:
            return vault_map[category]
        return "personal", ""

    if source == "user_correction":
        # Try regex routing
        if routing_rules:
            for rule in routing_rules:
                compiled = rule.get("_compiled")
                if compiled and compiled.search(text_lower):
                    return rule.get("wing", "personal"), rule.get("room", "")
        return "personal", ""

    # File-path based routing for RAG chunks
    file_path = (meta or {}).get("file_path", "")
    if file_path:
        path_lower = file_path.lower()
        if "profile" in path_lower or "identity" in path_lower:
            return "personal", ""
        if "medication" in path_lower or "health" in path_lower:
            return "personal", "health"
        if "routine" in path_lower or "pattern" in path_lower:
            return "personal", "routines"
        if "preference" in path_lower:
            return "jess", "preferences"
        if "finance" in path_lower or "budget" in path_lower:
            return "personal", "finance"

    # Try regex routing rules
    if routing_rules:
        for rule in routing_rules:
            compiled = rule.get("_compiled")
            if compiled and compiled.search(text_lower):
                return rule.get("wing", "personal"), rule.get("room", "")

    return "personal", ""


def _load_routing_rules(palace_yaml_path):
    """Load routing rules from palace.yaml."""
    import re as _re

    import yaml

    try:
        with open(palace_yaml_path) as f:
            config = yaml.safe_load(f) or {}
        rules = config.get("routing_rules", [])
        for rule in rules:
            try:
                rule["_compiled"] = _re.compile(rule["pattern"], _re.IGNORECASE)
            except Exception:
                pass
        return rules
    except Exception as e:
        print(f"Warning: could not load palace.yaml: {e}")
        return []


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
        help="Source ChromaDB collection name",
    )
    ap.add_argument(
        "--target-collection",
        default="",
        help="Target collection name (if different from source — for migration)",
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
    ap.add_argument(
        "--add-palace-metadata",
        action="store_true",
        help="Add wing/room metadata to documents based on content routing",
    )
    ap.add_argument(
        "--palace-yaml",
        default="data/palace.yaml",
        help="Path to palace.yaml for routing rules (default: data/palace.yaml)",
    )
    args = ap.parse_args()

    persist = Path(args.persist).expanduser().resolve()
    if not persist.exists():
        print(f"Error: persist directory does not exist: {persist}")
        sys.exit(1)

    target_collection_name = args.target_collection or args.collection

    print(f"ChromaDB path:    {persist}")
    print(f"Source collection: {args.collection}")
    print(f"Target collection: {target_collection_name}")
    print(f"Embed model:       {args.embed_model}")
    print(f"Batch size:        {args.batch_size}")
    print(f"Palace metadata:   {args.add_palace_metadata}")
    print(f"Dry run:           {args.dry_run}")
    print()

    # Load routing rules if palace metadata requested
    routing_rules = []
    if args.add_palace_metadata:
        routing_rules = _load_routing_rules(args.palace_yaml)
        print(f"Loaded {len(routing_rules)} routing rules from {args.palace_yaml}")

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
    model = SentenceTransformer(args.embed_model, trust_remote_code=True)
    print(f"Model loaded in {time.time() - t0:.1f}s")
    print(f"Embedding dimension: {model.get_sentence_embedding_dimension()}")

    if args.dry_run:
        print(f"\nDry run complete. {len(all_ids)} documents would be re-embedded.")
        print("No changes made.")
        return

    # Determine target collection
    if target_collection_name != args.collection:
        target = client.get_or_create_collection(name=target_collection_name)
        print(f"Target collection '{target_collection_name}' has {target.count()} existing docs")
    else:
        target = collection

    # Check if embedding dimension changed — need to recreate target
    new_dim = model.get_sentence_embedding_dimension()
    try:
        test_embed = model.encode(["test"], normalize_embeddings=True).tolist()
        target.query(query_embeddings=test_embed, n_results=1)
    except Exception as e:
        if "dimension" in str(e).lower():
            print(f"\nEmbedding dimension changed — recreating target collection...")
            client.delete_collection(name=target_collection_name)
            target = client.create_collection(name=target_collection_name)
            print(f"Collection recreated (new dimension: {new_dim})")
        else:
            raise

    # Re-embed and upsert in batches
    print(f"\nRe-embedding {len(all_ids)} documents into '{target_collection_name}'...")
    t0 = time.time()
    batches_done = 0
    palace_routed = 0

    for i in tqdm(range(0, len(all_ids), args.batch_size), desc="Re-indexing"):
        batch_ids = all_ids[i : i + args.batch_size]
        batch_docs = all_docs[i : i + args.batch_size]
        batch_metas = all_metas[i : i + args.batch_size]

        # Filter out None docs (shouldn't happen, but defensive)
        valid = [(id_, doc, meta) for id_, doc, meta in zip(batch_ids, batch_docs, batch_metas) if doc]
        if not valid:
            continue

        v_ids, v_docs, v_metas = zip(*valid)

        # Add palace metadata if requested
        if args.add_palace_metadata:
            updated_metas = []
            for doc, meta in zip(v_docs, v_metas):
                meta = dict(meta) if meta else {}
                wing, room = _route_to_wing_room(doc, meta, routing_rules)
                meta["wing"] = wing
                meta["room"] = room
                updated_metas.append(meta)
                if wing:
                    palace_routed += 1
            v_metas = updated_metas

        # Generate new embeddings
        embeddings = model.encode(list(v_docs), normalize_embeddings=True).tolist()

        # Upsert with new embeddings
        target.upsert(
            ids=list(v_ids),
            documents=list(v_docs),
            metadatas=list(v_metas),
            embeddings=embeddings,
        )
        batches_done += 1

    elapsed = time.time() - t0
    print(f"\nRe-indexing complete in {elapsed:.1f}s ({batches_done} batches)")
    print(f"Total documents in target: {target.count()}")
    if args.add_palace_metadata:
        print(f"Palace-routed documents: {palace_routed}")

    # Run test queries with new embeddings
    print("\n--- Test queries (new embeddings) ---")
    test_results = run_test_queries(target, model, TEST_QUERIES)
    for qr in test_results:
        print(f"\nQuery: {qr['query']}")
        for r in qr["results"]:
            print(f"  [{r['distance']:.4f}] {r['file_path']}: {r['preview'][:80]}...")

    print("\nDone! Verify results above look reasonable before deploying.")


if __name__ == "__main__":
    main()
