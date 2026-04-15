"""
Tests for orchestrator.rag_ingest chunk ID generation.

Regression coverage for the bug where a markdown file containing two
headings with the same text (e.g. "### Remote Focus Timer for Mobile"
appearing twice in the same document) produced identical chunk IDs,
causing chromadb's upsert() to reject the entire batch with
DuplicateIDError and the scheduled ingest to retry the same broken
batch every ~2 minutes forever.

The fix adds the section index to the chunk ID so repeated headings
are disambiguated.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def stubbed_shared(monkeypatch):
    """Stub shared.collection + shared.embedding_model so _run_ingest_sync
    does not touch a real chromadb instance or the sentence-transformers
    model. The collection mock tracks upsert calls for assertions.
    """
    from orchestrator import shared

    calls = {"upsert": []}

    fake_coll = MagicMock()
    # get() returns empty for both file_marker scan and the delete-by-file-path
    # pagination loop so the fixture behaves like a clean collection.
    fake_coll.get.return_value = {"ids": [], "metadatas": []}
    fake_coll.count.return_value = 0

    def _upsert(ids, documents, metadatas, embeddings):
        calls["upsert"].append(
            {
                "ids": list(ids),
                "documents": list(documents),
                "metadatas": list(metadatas),
            }
        )

    fake_coll.upsert.side_effect = _upsert

    fake_model = MagicMock()
    fake_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[[0.1] * 8]))

    # encode returns one fake vector per input; side_effect handles variable
    # batch sizes.
    def _encode(batch, normalize_embeddings=True):
        return MagicMock(tolist=MagicMock(return_value=[[0.1] * 8] * len(batch)))

    fake_model.encode.side_effect = _encode

    monkeypatch.setattr(shared, "collection", fake_coll)
    monkeypatch.setattr(shared, "embedding_model", fake_model)

    return {"collection": fake_coll, "model": fake_model, "calls": calls}


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_duplicate_h3_headings_produce_unique_ids(tmp_path, stubbed_shared, monkeypatch):
    """Two sections with identical titles must yield two distinct chunk IDs."""
    from orchestrator import rag_ingest

    src = tmp_path / "rag"
    src.mkdir()
    # The offending shape from the real bug: same H3 text twice, each with
    # enough body to force _chunk_text() to emit at least one chunk.
    _write(
        src / "current.md",
        "# Projects\n\n"
        "### Remote Focus Timer for Mobile\n\n"
        "First occurrence body paragraph describing iOS work.\n\n"
        "### Remote Focus Timer for Mobile\n\n"
        "Second occurrence body paragraph describing Android work.\n",
    )

    monkeypatch.setattr(rag_ingest, "_RAG_SOURCE", src)

    stats = rag_ingest._run_ingest_sync()

    # The bug caused upsert to raise DuplicateIDError before any chunks were
    # written; the fix must let the call succeed.
    assert stats["changed_files"] == 1
    assert stats["new_chunks"] > 0

    upsert_calls = stubbed_shared["calls"]["upsert"]
    assert len(upsert_calls) == 1
    all_ids = upsert_calls[0]["ids"]
    assert len(all_ids) == len(set(all_ids)), f"duplicate IDs in batch: {all_ids}"

    # Both sections must appear — one chunk each minimum, disambiguated by
    # section_index. Look at the metadata rather than parsing the ID string.
    chunk_metas = [m for m in upsert_calls[0]["metadatas"] if m.get("kind") == "chunk"]
    sec_indices = {m["section_index"] for m in chunk_metas if "section_index" in m}
    assert 0 in sec_indices and any(idx > 0 for idx in sec_indices), (
        f"expected distinct section_index values for repeated headings, got {sec_indices}"
    )


def test_unique_headings_still_work(tmp_path, stubbed_shared, monkeypatch):
    """Sanity: normal files with unique headings continue to ingest cleanly."""
    from orchestrator import rag_ingest

    src = tmp_path / "rag"
    src.mkdir()
    _write(
        src / "notes.md",
        "## Section A\n\nBody A paragraph.\n\n## Section B\n\nBody B paragraph.\n",
    )

    monkeypatch.setattr(rag_ingest, "_RAG_SOURCE", src)

    stats = rag_ingest._run_ingest_sync()

    assert stats["changed_files"] == 1
    assert stats["new_chunks"] > 0
    upsert_calls = stubbed_shared["calls"]["upsert"]
    assert len(upsert_calls) == 1
    ids = upsert_calls[0]["ids"]
    assert len(ids) == len(set(ids))


def test_batch_failure_is_isolated(tmp_path, stubbed_shared, monkeypatch):
    """A failing upsert batch must be logged but not crash the whole run."""
    from orchestrator import rag_ingest

    src = tmp_path / "rag"
    src.mkdir()
    _write(src / "one.md", "## A\n\nBody.\n")

    monkeypatch.setattr(rag_ingest, "_RAG_SOURCE", src)

    # Make the fake collection raise on upsert so we can prove the run
    # completes and logs an error instead of propagating.
    stubbed_shared["collection"].upsert.side_effect = RuntimeError("boom")

    with patch.object(rag_ingest.logger, "error") as mock_error:
        stats = rag_ingest._run_ingest_sync()

    # Run completed without raising; nothing was written.
    assert stats["new_chunks"] == 0
    # Error was logged with the expected prefix so the batch is identifiable.
    assert mock_error.called
    msg = mock_error.call_args[0][0]
    assert "Upsert batch failed" in msg


def test_middle_batch_failure_does_not_lose_neighbors(tmp_path, stubbed_shared, monkeypatch):
    """A failure in the middle of a batch sequence must not affect earlier or
    later batches. Regression coverage for the `upsert_ids[:written]` slice
    bug — with `_UPSERT_BATCH` clamped to 2, if a middle batch raises, the
    preceding and following batches must still be persisted and the chunk
    counter must reflect exactly the successful batches.
    """
    from orchestrator import rag_ingest

    src = tmp_path / "rag"
    src.mkdir()
    # Six H2 sections → six chunks, plus one file marker = 7 items. With
    # _UPSERT_BATCH=2 that's four batches: [2,2,2,1].
    body = "\n\n".join(f"## Section {i}\n\nBody paragraph for section {i}." for i in range(6))
    _write(src / "multi.md", body)

    monkeypatch.setattr(rag_ingest, "_RAG_SOURCE", src)
    monkeypatch.setattr(rag_ingest, "_UPSERT_BATCH", 2)

    # Only the 2nd upsert call raises; 1st, 3rd, 4th all succeed.
    call_count = {"n": 0}

    def _upsert_selective(ids, documents, metadatas, embeddings):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("middle batch boom")
        stubbed_shared["calls"]["upsert"].append(
            {
                "ids": list(ids),
                "documents": list(documents),
                "metadatas": list(metadatas),
            }
        )

    stubbed_shared["collection"].upsert.side_effect = _upsert_selective

    with patch.object(rag_ingest.logger, "error") as mock_error:
        stats = rag_ingest._run_ingest_sync()

    # Four batches attempted, one failed → three persisted (5 items: 4 chunks
    # + 1 file marker landed; chunks 2 and 3 did not).
    assert call_count["n"] == 4
    assert stats["new_chunks"] == 5
    # Exactly one error logged (the middle batch).
    assert mock_error.call_count == 1
    # The 1st, 3rd, and 4th batches all landed.
    persisted = stubbed_shared["calls"]["upsert"]
    assert len(persisted) == 3
    # Collapse persisted IDs and confirm section_index 0/1 and 4/5 survived
    # while 2/3 (the failed middle batch) did not.
    persisted_ids = {i for call in persisted for i in call["ids"]}
    persisted_sec_indices = sorted(
        int(i.split("::")[2].split(":")[0]) for i in persisted_ids if i.startswith("chunk::")
    )
    assert persisted_sec_indices == [0, 1, 4, 5], (
        f"expected section_indices [0,1,4,5], got {persisted_sec_indices} from {sorted(persisted_ids)}"
    )
