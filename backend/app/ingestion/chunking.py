"""Semantic chunking: splits by heading / numbered procedure / warning / table
rather than arbitrary character windows, per the plan's ingestion requirements.

Known limitation (documented in docs/PRODUCTION_READINESS.md): table-to-heading
attribution is best-effort (nearest heading seen so far on the page), because the
PDF extraction path does not currently carry each table's exact vertical position
relative to surrounding text. Good enough for retrieval filtering; not pixel-exact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.extracted import ExtractedDocument

TARGET_CHARS = 1200
MIN_CHARS = 150

# Independent review concern #16: 40 chunks in the corpus exceeded 2,000
# characters (largest over 11,000), all table/error-code chunks -- and an
# embedding model typically truncates its input, so rows past the truncation
# point are invisible to semantic search even though they were "indexed".
MAX_TABLE_CHUNK_CHARS = 1800

WARNING_RE = re.compile(r"^(WARNING|CAUTION|DANGER|NOTICE|IMPORTANT|LOCKOUT[/ ]TAGOUT)\b", re.IGNORECASE)
PROCEDURE_RE = re.compile(r"^\d{1,2}[\.\)]\s+\S")
ERROR_CODE_HEADING_RE = re.compile(
    r"error code|fault code|service code|alarm code|diagnostic|error message|fault message", re.IGNORECASE
)
ERROR_CODE_KEYWORDS_RE = re.compile(r"\b(error|fault|alarm|diagnostic)\b", re.IGNORECASE)
ERROR_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{1,3}-?\d{1,3}\b")


@dataclass
class ChunkRecord:
    page_number: int | None
    section_heading: str | None
    chunk_type: str  # text | procedure | warning | table | error_code
    content: str


def _classify_line(stripped: str) -> str:
    if WARNING_RE.match(stripped):
        return "warning"
    if PROCEDURE_RE.match(stripped):
        return "procedure"
    return "text"


def _norm_heading(s: str) -> str:
    """Collapse whitespace for heading comparison. Heading text comes from joined
    PDF text spans while body lines come from page.get_text(); the two can differ
    in internal spacing (this corpus renders some title text letter-spaced, e.g.
    'C M A D I S H M A C H I N E S'). Without normalizing, headings never match
    and chunking silently degrades to fixed-size windows."""
    return re.sub(r"\s+", " ", s).strip().lower()


def _chunk_page_text(page_number: int, text: str, headings: list[tuple[str, int]]) -> list[ChunkRecord]:
    heading_lookup = {_norm_heading(h[0]): h[0] for h in headings if h[0].strip()}
    lines = text.splitlines()

    records: list[ChunkRecord] = []
    current_heading: str | None = None
    buffer: list[str] = []
    buffer_type = "text"

    def flush():
        nonlocal buffer, buffer_type
        content = "\n".join(buffer).strip()
        if content:
            records.append(ChunkRecord(page_number, current_heading, buffer_type, content))
        buffer = []
        buffer_type = "text"

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        norm = _norm_heading(stripped)
        if norm in heading_lookup:
            flush()
            current_heading = heading_lookup[norm]
            continue

        line_type = _classify_line(stripped)
        if buffer and buffer_type != line_type:
            flush()
        buffer_type = line_type
        buffer.append(stripped)

        if buffer_type == "text" and sum(len(l) for l in buffer) > TARGET_CHARS:
            flush()

    flush()

    for rec in records:
        if rec.section_heading and ERROR_CODE_HEADING_RE.search(rec.section_heading):
            rec.chunk_type = "error_code"
        elif rec.chunk_type == "text" and _looks_like_error_code_listing(rec.content):
            rec.chunk_type = "error_code"

    return _merge_small_chunks(records)


def _looks_like_error_code_listing(content: str) -> bool:
    tokens = ERROR_CODE_TOKEN_RE.findall(content)
    return len(tokens) >= 3 and bool(ERROR_CODE_KEYWORDS_RE.search(content))


def _looks_like_error_code_table(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    header = " ".join(rows[0] or [])
    if ERROR_CODE_KEYWORDS_RE.search(header):
        return True
    # Fallback: many manuals list codes as a bare first column with no header
    # naming it explicitly (e.g. a two-column "E1 | Thermistor open" table).
    checked = matched = 0
    for row in rows[1:9]:
        if not row:
            continue
        cell = (row[0] or "").strip()
        if not cell:
            continue
        checked += 1
        if ERROR_CODE_TOKEN_RE.fullmatch(cell):
            matched += 1
    return checked >= 3 and matched / checked >= 0.5


def _render_table_window(header: list[str], rows: list[list[str]], label: str | None) -> str:
    lines = []
    if label:
        lines.append(label)
    lines.append("| " + " | ".join(c or "" for c in header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        lines.append("| " + " | ".join(c or "" for c in row) + " |")
    return "\n".join(lines)


def _split_table_rows(table: ExtractedTable) -> list[str]:
    """Bound each table chunk by row windows, not raw character count alone --
    a split must never land mid-row. The header row is repeated in every
    window (independent review: 'repeat the table title and column headers')
    so each chunk is independently understandable by retrieval and by a
    reader, instead of relying on an earlier chunk's now-truncated header."""
    if not table.rows:
        return []
    header = table.rows[0]
    body = table.rows[1:]
    if not body:
        return [table.as_markdown()]

    full = table.as_markdown()
    if len(full) <= MAX_TABLE_CHUNK_CHARS:
        return [full]

    header_overhead = len(" | ".join(c or "" for c in header)) + 10
    avg_row_len = max(1, (len(full) - header_overhead) // max(1, len(body)))
    rows_per_window = max(1, MAX_TABLE_CHUNK_CHARS // avg_row_len)

    windows = [body[i : i + rows_per_window] for i in range(0, len(body), rows_per_window)]
    total = len(windows)
    return [
        _render_table_window(header, window, f"(table, part {idx + 1} of {total})")
        for idx, window in enumerate(windows)
    ]


def _merge_small_chunks(records: list[ChunkRecord]) -> list[ChunkRecord]:
    merged: list[ChunkRecord] = []
    for rec in records:
        if (
            merged
            and len(rec.content) < MIN_CHARS
            and merged[-1].chunk_type == rec.chunk_type
            and merged[-1].section_heading == rec.section_heading
        ):
            merged[-1] = ChunkRecord(
                merged[-1].page_number,
                merged[-1].section_heading,
                merged[-1].chunk_type,
                merged[-1].content + "\n" + rec.content,
            )
        else:
            merged.append(rec)
    return merged


def chunk_document(extracted: ExtractedDocument) -> list[ChunkRecord]:
    all_records: list[ChunkRecord] = []
    for page in extracted.pages:
        if not page.text.strip() and not page.tables:
            continue
        page_records = _chunk_page_text(page.page_number, page.text, page.headings)
        all_records.extend(page_records)

        last_heading = page_records[-1].section_heading if page_records else None
        for table in page.tables:
            if not table.rows:
                continue
            is_error_table = _looks_like_error_code_table(table.rows) or (
                last_heading and ERROR_CODE_HEADING_RE.search(last_heading)
            )
            chunk_type = "error_code" if is_error_table else "table"
            for window_md in _split_table_rows(table):
                if window_md.strip():
                    all_records.append(
                        ChunkRecord(page.page_number, last_heading, chunk_type, window_md)
                    )

    return all_records
