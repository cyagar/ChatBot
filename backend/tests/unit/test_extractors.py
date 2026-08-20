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
