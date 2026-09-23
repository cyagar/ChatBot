from app.ingestion.chunking import chunk_document
from app.ingestion.extracted import ExtractedDocument, ExtractedPage, ExtractedTable


def _doc(text, headings=None, tables=None):
    page = ExtractedPage(page_number=1, text=text, headings=headings or [], tables=tables or [])
    return ExtractedDocument(status="ok", reason=None, pages=[page])


def test_splits_on_heading_boundary():
    text = "Overview\nSome intro text about the machine.\nInstallation\nStep by step content here."
    doc = _doc(text, headings=[("Overview", 0), ("Installation", 0)])
    records = chunk_document(doc)
    headings = {r.section_heading for r in records}
    assert "Overview" in headings
    assert "Installation" in headings


def test_heading_match_is_whitespace_normalized():
    """Regression: heading text built by joining PDF text spans can carry a
    different number of internal spaces than the same title as it appears in
    page.get_text() (e.g. extra space from an inter-span gap: 'CMA  DISHMACHINES'
    vs 'CMA DISHMACHINES'). Without normalizing both sides, the heading boundary
    is silently missed and chunking falls back to undifferentiated text."""
    text = "CMA DISHMACHINES\nSpecifications table follows below in this section."
    doc = _doc(text, headings=[("CMA  DISHMACHINES", 0)])  # double space, as a span-join artifact
    records = chunk_document(doc)
    assert any(r.section_heading == "CMA  DISHMACHINES" for r in records)


def test_numbered_steps_become_procedure_chunk():
    text = "1. Disconnect power.\n2. Remove the front panel.\n3. Replace the inlet valve."
    doc = _doc(text)
    records = chunk_document(doc)
    assert any(r.chunk_type == "procedure" for r in records)


def test_warning_line_becomes_warning_chunk():
    text = "WARNING: Disconnect power before servicing. Risk of electric shock."
    doc = _doc(text)
    records = chunk_document(doc)
    assert any(r.chunk_type == "warning" for r in records)


def test_p1_16_a_warnings_continuation_lines_stay_in_the_same_chunk():
    """P1-16 (external review, 2026-09-21): _classify_line only recognizes a
    warning's OWN marker line ("WARNING: ..."), never its continuation --
    every line after it used to classify as plain 'text' and immediately
    flush(), detaching the warning label from its own body/safety content
    into a separate, unlabeled chunk one line later."""
    text = (
        "WARNING: Disconnect power before servicing.\n"
        "Risk of electric shock if this step is skipped.\n"
        "Wait 5 minutes before opening the panel."
    )
    doc = _doc(text)
    records = chunk_document(doc)
    warning_records = [r for r in records if r.chunk_type == "warning"]
    assert len(warning_records) == 1, (
        f"expected one merged warning chunk, got {len(warning_records)}: {[r.content for r in warning_records]}"
    )
    assert "Risk of electric shock" in warning_records[0].content
    assert "Wait 5 minutes" in warning_records[0].content


def test_p1_16_a_procedure_steps_continuation_lines_stay_with_that_step():
    text = (
        "1. Disconnect power.\n"
        "Make sure the breaker is fully off before proceeding.\n"
        "2. Remove the front panel."
    )
    doc = _doc(text)
    records = chunk_document(doc)
    procedure_records = [r for r in records if r.chunk_type == "procedure"]
    assert len(procedure_records) == 1
    assert "Make sure the breaker is fully off" in procedure_records[0].content


def test_table_under_error_code_heading_is_classified_error_code():
    table = ExtractedTable(page_number=1, rows=[["Code", "Meaning"], ["E1", "Thermistor open"], ["E2", "Thermistor shorted"]])
    doc = _doc("Fault Codes\nSee table below.", headings=[("Fault Codes", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if r.chunk_type == "error_code"]
    assert table_records, "table under a fault-code heading should be classified error_code"


def test_bare_code_column_table_detected_without_heading_keyword():
    """Regression: manuals often list codes as a bare first column with no
    header naming it 'error'/'fault' explicitly."""
    table = ExtractedTable(
        page_number=1,
        rows=[["", "Meaning"], ["E1", "Thermistor open"], ["E2", "Thermistor shorted"], ["E3", "Heater relay failure"]],
    )
    doc = _doc("Diagnostics\nSee table below.", headings=[("Diagnostics", 0)], tables=[table])
    records = chunk_document(doc)
    assert any(r.chunk_type == "error_code" for r in records)


def test_plain_table_without_code_signals_stays_table_type():
    table = ExtractedTable(page_number=1, rows=[["Part", "Qty"], ["Gasket", "1"], ["Screw", "4"]])
    doc = _doc("Parts List\nSee table below.", headings=[("Parts List", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if "Gasket" in r.content]
    assert table_records and table_records[0].chunk_type == "table"


def test_large_table_is_split_into_bounded_windows_with_header_repeated():
    """Independent review concern #16: the corpus's largest table chunk was
    over 11,000 characters, and an embedding model typically truncates its
    input -- later rows were 'indexed' but invisible to semantic search."""
    header = ["Code", "Meaning", "Corrective Action"]
    rows = [header] + [[f"E{i}", f"Fault description number {i} " * 3, f"Corrective action steps for fault {i}"] for i in range(200)]
    table = ExtractedTable(page_number=1, rows=rows)
    doc = _doc("Error Codes\nSee table below.", headings=[("Error Codes", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if r.chunk_type == "error_code" and "Code" in r.content]

    assert len(table_records) > 1, "a table this large must be split into more than one chunk"
    for rec in table_records:
        assert len(rec.content) <= 1800 + 200  # cap plus small header/label overhead
        assert "| Code | Meaning | Corrective Action |" in rec.content, "header must repeat in every window"

    # No row's data was dropped in the split.
    combined = "\n".join(r.content for r in table_records)
    for i in (0, 50, 150, 199):
        assert f"E{i}" in combined


def test_single_oversized_row_is_split_across_cells_without_losing_values():
    """P1-13 (independent follow-up review): the old row-window arithmetic used
    `max(1, ...)`, guaranteeing at least one row per window -- so a single row
    bigger than the whole budget still produced one over-limit chunk whose tail
    the embedding model would truncate away. The row must now be split across
    cells, with exact values preserved (never truncated)."""
    header = ["Code", "Meaning", "Corrective Action"]
    huge_cell_a = "A" * 1500 + " PARTNUM-11111"
    huge_cell_b = "B" * 1500 + " PARTNUM-22222"
    rows = [header, ["E99", huge_cell_a, huge_cell_b]]
    table = ExtractedTable(page_number=1, rows=rows)
    doc = _doc("Error Codes\nSee table below.", headings=[("Error Codes", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if "PARTNUM-" in r.content or "E99" in r.content]

    assert len(table_records) > 1, "an oversized single row must be split, not emitted whole"
    for rec in table_records:
        assert len(rec.content) <= 1800 + 200, f"chunk still over the limit: {len(rec.content)}"
        assert "| Code | Meaning | Corrective Action |" in rec.content, "header must repeat in every piece"

    # Exact values survive verbatim -- nothing truncated mid-cell.
    combined = "\n".join(r.content for r in table_records)
    assert "PARTNUM-11111" in combined
    assert "PARTNUM-22222" in combined
    assert "E99" in combined


def test_p1_16_split_row_repeats_the_row_identifier_in_every_piece():
    """P1-16 (external review, 2026-09-21): a long remedy/description in a
    LATER column used to push the split so the row's own identifier (column
    0 -- the error code, in this test) only survived in the first piece. A
    retrieved later piece had no way to tell which code its remedy was for."""
    header = ["Code", "Meaning", "Corrective Action"]
    huge_remedy = "Check the following in order: " + ("step detail " * 200)
    rows = [header, ["E77", "Heater relay failure", huge_remedy]]
    table = ExtractedTable(page_number=1, rows=rows)
    doc = _doc("Error Codes\nSee table below.", headings=[("Error Codes", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if r.chunk_type == "error_code" and "Code" in r.content]

    assert len(table_records) > 1, "the remedy must be split across more than one piece"
    for rec in table_records:
        assert "E77" in rec.content, f"row identifier missing from a split piece: {rec.content[:120]}"


def test_single_cell_larger_than_the_budget_is_kept_whole_not_truncated():
    """A cell that alone exceeds the budget is emitted whole on its own: an
    exact part number or measured value must never be silently corrupted by a
    mid-cell cut, even at the cost of one over-budget chunk."""
    header = ["Code", "Detail"]
    monster = "X" * 4000 + " CRITICAL-VALUE-42"
    table = ExtractedTable(page_number=1, rows=[header, ["E1", monster]])
    doc = _doc("Error Codes\nSee table below.", headings=[("Error Codes", 0)], tables=[table])
    records = chunk_document(doc)
    combined = "\n".join(r.content for r in records)
    assert "CRITICAL-VALUE-42" in combined, "an oversized cell must not be truncated"


def test_small_table_is_not_split():
    table = ExtractedTable(page_number=1, rows=[["Part", "Qty"], ["Gasket", "1"], ["Screw", "4"]])
    doc = _doc("Parts List\nSee table below.", headings=[("Parts List", 0)], tables=[table])
    records = chunk_document(doc)
    table_records = [r for r in records if "Gasket" in r.content]
    assert len(table_records) == 1


def test_small_adjacent_chunks_of_same_type_are_merged():
    text = "Intro\nA short line.\nB.\nAnother short one."
    doc = _doc(text, headings=[("Intro", 0)])
    records = chunk_document(doc)
    # Should not fragment into many <150-char chunks of the same type/heading.
    text_records = [r for r in records if r.chunk_type == "text" and r.section_heading == "Intro"]
    assert len(text_records) <= 1
