from app.ingestion.dedup import containment, shingles


def test_shingles_short_text_returns_single_shingle():
    assert shingles("hello world") == {"hello world"}


def test_identical_text_has_containment_one():
    text = "The quick brown fox jumps over the lazy dog repeatedly and often."
    assert containment(shingles(text), shingles(text)) == 1.0


def test_unrelated_text_has_low_containment():
    a = shingles("Bunn Axiom coffee brewer installation and operating guide for technicians.")
    b = shingles("CMA 180UC dishmachine owner's manual rinse arm cleaning instructions section.")
    assert containment(a, b) < 0.1


def test_containment_is_length_robust():
    """A short revision's text fully contained in a much longer revision (extra
    appendices/front matter) scores high, unlike a symmetric overlap measure
    that the length mismatch would drag down (ULTRA NX guides differ 48pp vs 27pp)."""
    short = "Ultra NX frozen beverage dispenser installation and operating guide. " * 5
    long_doc = short + ("Appendix: wiring diagrams and additional specification tables. " * 40)

    assert containment(shingles(short), shingles(long_doc)) > 0.9
