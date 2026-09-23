"""Serves the source manual files and per-page evidence images.

Citations link here so a technician can open the exact cited manual, and the
"View manual evidence" panel renders a page image without requiring a full PDF
viewer library. Files are served from object storage by content-addressed name
(sha256-derived), never by trusting a client-supplied path, and every response
sets a strict content type — no user-controlled path traversal is possible since
the id is a DB primary key, not a filename.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse

from app.auth.deps import CurrentUser, get_current_user
from app.config import get_settings
from app.db import get_conn

router = APIRouter(prefix="/api/manuals", tags=["manuals"])

_MIME_BY_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_PIL_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}


def _sniff_image_mime(path) -> str:
    """P1-18 (independent follow-up review): file_type='image' collapses
    every raster format ingestion accepts into one bucket (see
    app/ingestion/extractors.py's sniff_file_type) -- serving it as a
    hardcoded 'image/jpeg' mislabeled any PNG/GIF/etc file. Sniff the real
    format from the bytes actually on disk instead of trusting the coarse
    ingestion-time bucket."""
    from PIL import Image

    try:
        with Image.open(path) as img:
            return _PIL_FORMAT_TO_MIME.get(img.format or "", "application/octet-stream")
    except Exception:
        return "application/octet-stream"


def _get_document(document_id: int, *, allow_unapproved: bool = False):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, original_filename, storage_path, file_type, status "
            "FROM documents WHERE id = %s AND deactivated_at IS NULL",
            (document_id,),
        ).fetchone()
        if row is not None and not allow_unapproved:
            approved = conn.execute(
                "SELECT 1 FROM documents WHERE id = %s AND review_status = 'approved'",
                (document_id,),
            ).fetchone()
            if approved is None:
                row = None
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Manual not found.")
    return row


@router.get("/{document_id}/file")
def get_manual_file(document_id: int, user: CurrentUser = Depends(get_current_user)):
    """Serves the raw file. The frontend appends #page=N (browser-native PDF
    navigation, supported by Chrome/Edge/Safari, including iPadOS) to deep-link
    to the cited page without needing a bundled PDF.js viewer.

    Gated on review_status='approved' for everyone except administrators --
    the raw file and evidence endpoints are a second path to document content
    that must honor the same P0-6 approval boundary as retrieval, not just be
    reachable via an old citation or a guessed document id (concern #6)."""
    doc = _get_document(document_id, allow_unapproved=user.role == "administrator")
    settings = get_settings()
    path = settings.local_storage_dir_resolved / doc["storage_path"]
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Stored file is missing.")
    if doc["file_type"] == "image":
        mime = _sniff_image_mime(path)
    else:
        mime = _MIME_BY_TYPE.get(doc["file_type"], "application/octet-stream")
    # P1-18 (independent follow-up review): a hand-built
    # `f'inline; filename="{name}"'` header string let a stored filename
    # containing a `"` break out of the quoted parameter (and, depending on
    # the ASGI server, a control character could reach the raw header).
    # FileResponse's own `filename=`/`content_disposition_type=` builds this
    # header using Starlette's RFC 6266-aware encoding instead of raw
    # string interpolation.
    return FileResponse(
        path,
        media_type=mime,
        filename=doc["original_filename"],
        content_disposition_type="inline",
    )


@lru_cache(maxsize=256)
def _render_page_png(document_id: int, storage_path: str, page_number: int) -> bytes:
    import fitz  # local import: this module is only needed when a PDF page is requested

    settings = get_settings()
    path = settings.local_storage_dir_resolved / storage_path
    doc = fitz.open(path)
    try:
        if not (1 <= page_number <= doc.page_count):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page out of range.")
        pix = doc[page_number - 1].get_pixmap(dpi=150)
        return pix.tobytes("png")
    finally:
        doc.close()


@router.get("/{document_id}/pages/{page_number}/image")
def get_page_image(document_id: int, page_number: int, user: CurrentUser = Depends(get_current_user)):
    doc = _get_document(document_id, allow_unapproved=user.role == "administrator")
    if doc["file_type"] != "pdf":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Page images are only available for PDF manuals.")
    png_bytes = _render_page_png(doc["id"], doc["storage_path"], page_number)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/{document_id}/chunks/{chunk_id}/evidence")
def get_evidence(document_id: int, chunk_id: int, user: CurrentUser = Depends(get_current_user)):
    """Backs the 'View manual evidence' expandable panel: the exact excerpt text
    plus (for PDFs) a link to the rendered page image.

    Gated on review_status='approved' for everyone except administrators, same
    as get_manual_file -- this is a second path to full chunk content (concern #6)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.id, c.content, c.page_number, c.section_heading, c.chunk_type, "
            "d.original_filename, d.title, d.revision, d.doc_type, d.file_type, d.is_current_revision "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.id = %s AND c.document_id = %s AND d.deactivated_at IS NULL"
            + ("" if user.role == "administrator" else " AND d.review_status = 'approved'"),
            (chunk_id, document_id),
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evidence not found.")
    return {
        "chunk_id": row["id"],
        "content": row["content"],
        "chunk_type": row["chunk_type"],
        "page_number": row["page_number"],
        "section_heading": row["section_heading"],
        "filename": row["original_filename"],
        "title": row["title"],
        "revision": row["revision"],
        "doc_type": row["doc_type"],
        "is_current_revision": bool(row["is_current_revision"]),
        "has_page_image": row["file_type"] == "pdf" and row["page_number"] is not None,
    }
