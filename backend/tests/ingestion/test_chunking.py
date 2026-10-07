"""_drop_repeated_boilerplate must collapse a genuinely repeated running
header/footer down to one occurrence, but never a warning, procedure, table
or error_code chunk, no matter how many pages it recurs on."""
from __future__ import annotations

from app.ingestion.chunking import ChunkRecord, _drop_repeated_boilerplate


def _rec(page: int, chunk_type: str, content: str) -> ChunkRecord:
    return ChunkRecord(page_number=page, section_heading=None, chunk_type=chunk_type, content=content)


def test_a_repeated_running_header_is_kept_once_and_dropped_elsewhere():
    records = [_rec(p, "text", "MODEL CMA-180UC PARTS MANUAL Rev. 1.20C") for p in (1, 2, 3, 4)]
    kept = _drop_repeated_boilerplate(records)
    assert len(kept) == 1
    assert kept[0].page_number == 1


def test_a_repeated_caution_footer_survives_even_though_it_recurs_across_pages():
    records = [_rec(p, "text", "CAUTION: Hot surface, do not touch.") for p in (1, 2, 3, 4)]
    kept = _drop_repeated_boilerplate(records)
    assert len(kept) == 4


def test_a_repeated_warning_typed_chunk_survives():
    records = [_rec(p, "warning", "WARNING: Disconnect power before servicing.") for p in (1, 2, 3)]
    kept = _drop_repeated_boilerplate(records)
    assert len(kept) == 3


def test_a_repeated_table_typed_chunk_survives():
    records = [_rec(p, "table", "ITEM NO. REQ'D P/N") for p in (1, 2, 3)]
    kept = _drop_repeated_boilerplate(records)
    assert len(kept) == 3


def test_a_header_repeated_on_only_two_pages_is_not_boilerplate():
    records = [_rec(p, "text", "Some section heading") for p in (1, 2)]
    kept = _drop_repeated_boilerplate(records)
    assert len(kept) == 2
