import zipfile

from app.ingestion.extractors import classify_extension, extract, resolve_file_type, sniff_file_type


def test_classify_extension_maps_known_types(tmp_path):
    assert classify_extension(tmp_path / "x.pdf") == "pdf"
    assert classify_extension(tmp_path / "x.doc") == "doc"
    assert classify_extension(tmp_path / "x.indd") == "indd"
    assert classify_extension(tmp_path / "x.jpg") == "image"
    assert classify_extension(tmp_path / "x.weird") == "unknown"


def test_pdf_disguised_as_doc_is_detected_by_magic_bytes(make_pdf, tmp_path):
    """Regression: the real corpus contains 5 files that are actual PDFs saved
    with a .doc/.docx extension. Content must win over the filename."""
    pdf_path = make_pdf(["Some manual text on the first page."])
    disguised = tmp_path / "renamed_as_legacy.doc"
    disguised.write_bytes(pdf_path.read_bytes())

    effective_type, note = resolve_file_type(disguised)
    assert effective_type == "pdf"
    assert note is not None and "pdf" in note.lower()


def test_extract_dispatches_to_pdf_extractor_for_disguised_file(make_pdf, tmp_path):
    pdf_path = make_pdf(["Real content: replace the inlet valve per section 4."])
    disguised = tmp_path / "instructions.docx"
    disguised.write_bytes(pdf_path.read_bytes())

    file_type, extracted, mismatch = extract(disguised)
    assert file_type == "pdf"
    assert extracted.status == "ok"
    assert "inlet valve" in extracted.pages[0].text
    assert mismatch is not None


def test_sniff_file_type_returns_none_for_empty_file(tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert sniff_file_type(empty) is None


def test_real_extension_with_matching_content_has_no_mismatch_note(make_pdf):
    pdf_path = make_pdf(["Normal PDF content."])
    effective_type, note = resolve_file_type(pdf_path)
    assert effective_type == "pdf"
    assert note is None


def test_extract_pdf_with_text_layer_is_ok_status(make_pdf):
    pdf_path = make_pdf(["Page one has plenty of real extractable text content here."])
    _, extracted, _ = extract(pdf_path)
    assert extracted.status == "ok"
    assert extracted.total_chars > 0


def test_extract_scanned_pdf_without_ocr_is_unsupported(tmp_path):
    import fitz

    doc = fitz.open()
    doc.new_page()  # blank page: no text layer at all
    path = tmp_path / "scanned.pdf"
    doc.save(path)
    doc.close()

    _, extracted, _ = extract(path, ocr_available=False)
    assert extracted.status == "unsupported"
    assert "OCR" in extracted.reason or "text layer" in extracted.reason


def test_p1_09_a_real_legacy_doc_is_classified_and_parsed_as_doc(tmp_path):
    """P1-09 (external review, 2026-09-21): OLE files (.doc/.xls/.ppt all
    share the same compound-file magic bytes) used to sniff as the generic
    "ole" kind unconditionally -- resolve_file_type trusts the sniff over
    the .doc extension, and extract() only ever dispatches to
    extract_legacy_doc for a "doc" file_type, never "ole". A genuine binary
    .doc therefore always took the unsupported branch, contradicting the
    documented "legacy .doc supported" claim. Reproduced before the fix:
    this exact file resolved to effective_type "ole" and extract() returned
    status="unsupported" instead of parsing it."""
    from tests.ingestion.ole_fixtures import make_ole_file, word_stream_bytes

    data = word_stream_bytes([
        "This is the real body text of a legacy Word document about a "
        "coffee brewer's thermostat calibration procedure."
    ])
    path = tmp_path / "manual.doc"
    path.write_bytes(make_ole_file("WordDocument", data))

    assert sniff_file_type(path) == "doc"
    effective_type, note = resolve_file_type(path)
    assert effective_type == "doc"
    assert note is None, "a real .doc matching its extension must not be flagged as a mismatch"

    file_type, extracted, _ = extract(path)
    assert file_type == "doc"
    assert extracted.status == "partial"
    assert "thermostat calibration" in extracted.pages[0].text


def test_p1_09_a_non_word_ole_container_named_doc_stays_unsupported(tmp_path):
    """Companion to the test above: an OLE compound file that is NOT a Word
    document (no WordDocument stream -- e.g. a legacy .xls saved/renamed
    with a .doc extension) must not be misparsed as one just because it
    shares the outer OLE signature."""
    from tests.ingestion.ole_fixtures import make_ole_file, word_stream_bytes

    data = word_stream_bytes(["This OLE container is spreadsheet-shaped, not a Word document."])
    path = tmp_path / "not_actually_word.doc"
    path.write_bytes(make_ole_file("Workbook", data))

    assert sniff_file_type(path) == "ole"
    effective_type, note = resolve_file_type(path)
    assert effective_type == "ole"
    assert note is not None

    file_type, extracted, _ = extract(path)
    assert extracted.status == "unsupported", (
        "a non-Word OLE container must not be routed through extract_legacy_doc"
    )


def _make_ooxml(path, member_name):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member_name, "<xml/>")
        zf.writestr("[Content_Types].xml", "<Types/>")


def test_p1_09_xlsx_and_pptx_are_not_misclassified_as_docx(tmp_path):
    """P1-09: .docx/.xlsx/.pptx are all ZIP containers sharing the same
    magic bytes -- every ZIP OOXML file used to be labeled 'docx'
    unconditionally, so an .xlsx or .pptx could be sent to the docx parser.
    Distinguished by the one member file each format's own spec
    guarantees."""
    docx_path = tmp_path / "real.docx"
    _make_ooxml(docx_path, "word/document.xml")
    assert sniff_file_type(docx_path) == "docx"

    xlsx_path = tmp_path / "real.xlsx"
    _make_ooxml(xlsx_path, "xl/workbook.xml")
    assert sniff_file_type(xlsx_path) == "xlsx"
    file_type, extracted, _ = extract(xlsx_path)
    assert file_type == "xlsx"
    assert extracted.status == "unsupported", "an xlsx must not be routed through the docx parser"

    pptx_path = tmp_path / "real.pptx"
    _make_ooxml(pptx_path, "ppt/presentation.xml")
    assert sniff_file_type(pptx_path) == "pptx"
