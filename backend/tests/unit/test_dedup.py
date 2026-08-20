from app.ingestion.dedup import containment, jaccard, shingles


def test_shingles_short_text_returns_single_shingle():
    assert shingles("hello world") == {"hello world"}


def test_identical_text_has_similarity_one():
    text = "The quick brown fox jumps over the lazy dog repeatedly and often."
    a, b = shingles(text), shingles(text)
    assert jaccard(a, b) == 1.0
    assert containment(a, b) == 1.0


def test_unrelated_text_has_low_similarity():
    a = shingles("Bunn Axiom coffee brewer installation and operating guide for technicians.")
    b = shingles("CMA 180UC dishmachine owner's manual rinse arm cleaning instructions section.")
    assert jaccard(a, b) < 0.1
    assert containment(a, b) < 0.1


def test_containment_is_length_robust_unlike_jaccard():
    """A short revision's text fully contained in a much longer revision (extra
    appendices/front matter) should score high on containment even though
    Jaccard punishes the length mismatch — this is the exact failure mode found
    in the real corpus (ULTRA NX guides differ 48pp vs 27pp)."""
    short = "Ultra NX frozen beverage dispenser installation and operating guide. " * 5
    long_doc = short + ("Appendix: wiring diagrams and additional specification tables. " * 40)

    a, b = shingles(short), shingles(long_doc)
    assert containment(a, b) > 0.9
    assert jaccard(a, b) < containment(a, b)
