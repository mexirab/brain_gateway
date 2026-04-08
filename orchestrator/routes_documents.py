"""Document Vault API routes."""

import logging
import os
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import shared

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/documents")
async def list_docs(category: str = "", search: str = "", limit: int = 50, offset: int = 0):
    """List documents with optional filtering."""
    from state_store import list_documents

    docs = list_documents(category=category or None, search=search or None, limit=min(limit, 200), offset=offset)
    # Don't send full extracted_text in list view
    for d in docs:
        d.pop("extracted_text", None)
    return JSONResponse(docs)


@router.get("/api/documents/categories")
async def document_categories():
    """Document counts per category."""
    from state_store import get_document_categories

    return JSONResponse(get_document_categories())


@router.get("/api/documents/{doc_id}")
async def get_doc(doc_id: str):
    """Get a single document with full metadata."""
    from state_store import get_document

    doc = get_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(doc)


@router.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    category: str = Form("other"),
    tags: str = Form(""),
    notes: str = Form(""),
):
    """Upload a document to the vault."""
    import uuid

    from document_processor import extract_text, save_uploaded_file, validate_upload
    from state_store import save_document

    file_bytes = await file.read()
    filename = file.filename or "upload"

    # Validate
    error = validate_upload(file_bytes, filename, category)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Save file to disk
    relative_path = save_uploaded_file(file_bytes, filename, category)

    # Extract text
    from document_processor import get_full_path

    extracted = extract_text(get_full_path(relative_path))

    # Index in RAG
    rag_doc_id = None
    try:
        doc_uuid = str(uuid.uuid4())
        rag_doc_id = f"vault_{doc_uuid}"
        if extracted and shared.collection is not None:
            # Chunk long documents
            chunks = _chunk_text(extracted, 2000, 200)
            ids = [f"{rag_doc_id}_chunk_{i}" for i in range(len(chunks))]
            embeddings = [shared.embedding_model.encode(c, normalize_embeddings=True).tolist() for c in chunks]
            metadatas = [
                {
                    "source": "document_vault",
                    "category": category,
                    "title": title,
                    "vault_doc_id": doc_uuid,
                    "kind": "chunk",
                }
                for _ in chunks
            ]
            shared.collection.add(documents=chunks, embeddings=embeddings, metadatas=metadatas, ids=ids)
            logger.info(f"[DOCVAULT] Indexed {len(chunks)} chunks for '{title}'")
    except Exception as e:
        logger.warning(f"[DOCVAULT] RAG indexing failed: {e}")

    # Save metadata to SQLite
    now = datetime.now().isoformat()
    ext = os.path.splitext(filename)[1].lower()
    doc = save_document(
        {
            "id": doc_uuid,
            "title": title,
            "category": category,
            "tags": tags,
            "notes": notes,
            "file_name": filename,
            "file_path": relative_path,
            "file_type": ext,
            "file_size": len(file_bytes),
            "extracted_text": extracted,
            "rag_doc_id": rag_doc_id,
            "uploaded_at": now,
            "updated_at": now,
        }
    )

    logger.info(f"[DOCVAULT] Uploaded '{title}' ({category}, {len(file_bytes)} bytes, {len(extracted)} chars text)")
    doc.pop("extracted_text", None)
    return JSONResponse(doc)


@router.put("/api/documents/{doc_id}")
async def update_doc(doc_id: str, request: Request):
    """Update document metadata."""
    from state_store import update_document

    body = await request.json()
    ok = update_document(doc_id, body)
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/documents/{doc_id}")
async def delete_doc(doc_id: str):
    """Delete a document (file + metadata + RAG)."""
    from document_processor import delete_file
    from state_store import delete_document

    doc = delete_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Delete file from disk
    delete_file(doc["file_path"])

    # Remove from RAG
    try:
        if doc.get("rag_doc_id") and shared.collection is not None:
            # Delete all chunks with matching prefix
            all_ids = shared.collection.get(where={"vault_doc_id": doc["id"]})
            if all_ids and all_ids["ids"]:
                shared.collection.delete(ids=all_ids["ids"])
    except Exception as e:
        logger.warning(f"[DOCVAULT] RAG cleanup failed: {e}")

    return JSONResponse({"ok": True})


@router.get("/api/documents/{doc_id}/download")
async def download_doc(doc_id: str):
    """Download the original document file."""
    from document_processor import get_full_path
    from state_store import get_document

    doc = get_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Not found"}, status_code=404)
    full_path = get_full_path(doc["file_path"])
    if not os.path.exists(full_path):
        return JSONResponse({"error": "File missing"}, status_code=404)
    return FileResponse(full_path, filename=doc["file_name"])


@router.get("/api/documents/{doc_id}/text")
async def get_doc_text(doc_id: str):
    """Get extracted text for a document."""
    from state_store import get_document

    doc = get_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"text": doc.get("extracted_text") or ""})


def _chunk_text(text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
    """Split text into chunks with overlap."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
    return chunks or [text]
