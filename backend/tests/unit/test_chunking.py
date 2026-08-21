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
