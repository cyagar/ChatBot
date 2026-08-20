from app.ingestion.extracted import ExtractedDocument, ExtractedPage
from app.ingestion.metadata import extract_metadata


def _doc(*page_texts):
    pages = [ExtractedPage(page_number=i + 1, text=t) for i, t in enumerate(page_texts)]
    return ExtractedDocument(status="ok", reason=None, pages=pages)


def test_detects_bunn_axiom():
    doc = _doc("Axiom\nAutomatic Coffee Brewers\nBunn-O-Matic Corporation\nINSTALLATION & OPERATING GUIDE")
    meta = extract_metadata("axiom-guide.pdf", doc)
    assert meta.manufacturer == "Bunn-O-Matic Corporation"
    assert meta.doc_type == "installation_operating"
    assert any(m.model_name == "Axiom" for m in meta.machine_matches)


def test_detects_cma_dishmachine_despite_generic_filename():
    """Regression case from the real corpus: 'Order Entry.pdf' is actually a
    CMA EAH/EC/3-Door owner's manual — metadata must come from content, not name."""
    doc = _doc("www.cmadishmachines.com\nMODELS\nEAH/EC/3-Door\nInstallation and Operation\nRevision 1.03")
    meta = extract_metadata("Order Entry.pdf", doc)
    assert meta.manufacturer == "CMA Dishmachines"
    assert any(m.model_name == "EAH/EC/3-Door" for m in meta.machine_matches)


def test_detects_ads_glasswasher():
    doc = _doc("American Dish Service\nADS GLASSWASHER\nMODEL: ASQ II\nPARTS MANUAL")
    meta = extract_metadata("ads.pdf", doc)
    assert meta.manufacturer == "American Dish Service"
    assert meta.doc_type == "parts"
    assert any(m.model_name == "ASQ II" for m in meta.machine_matches)


def test_unknown_manufacturer_is_flagged_not_guessed():
    doc = _doc("Some generic text with no identifying manufacturer markers at all.")
    meta = extract_metadata("mystery.pdf", doc)
    assert meta.manufacturer is None
    assert meta.machine_matches == []
    assert any("review" in n.lower() for n in meta.notes)


def test_trademark_boilerplate_does_not_cause_false_machine_matches():
    """Regression: a real Bunn manual (iMIX Service & Repair) was falsely linked
    to Axiom and TF DBC because Bunn's standard legal notice lists every BUNN
    product name as a trademark: '...AutoPOD, AXIOM, ... Smart Funnel, ...
    Ultra are either trademarks or registered trademarks of Bunn-O-Matic
    Corporation.' Only iMIX should match here — it's the only one named outside
    the trademark list."""
    # Line-wrapped like real PDF extraction (commas don't align with line breaks) so
    # this actually exercises the backward multi-line expansion, not just a
    # single-line drop.
    trademark_boilerplate = (
        "392, A Partner You Can Count On, Air Infusion, AutoPOD, AXIOM, BrewLOGIC, BrewMETER,\n"
        "Brew Better Not Bitter, BrewWISE, BrewWIZARD, BUNN Espress, Cool Froth, DBC, Dual,\n"
        "Easy Pour, EasyClear, EasyGard, FlavorGard, Gourmet Ice, Gourmet Juice, High Intensity,\n"
        "iMIX, Infusion Series, Intellisteam, My Cafe, Phase Brew, PowerLogic, Scale-Pro,\n"
        "Silver Series, Single, Smart Funnel, Smart Hopper, SmartWAVE, Soft Heat, SplashGard,\n"
        "ThermoFresh, Titan, trifecta, Velocity Brew, Ultra are either trademarks or registered\n"
        "trademarks of Bunn-O-Matic Corporation."
    )
    doc = _doc(
        "Bunn-O-Matic Corporation\nSERVICE & REPAIR MANUAL\nIMIX & IMIX-S+",
        trademark_boilerplate,
    )
    meta = extract_metadata("imix-service-manual.pdf", doc)
    matched_models = {m.model_name for m in meta.machine_matches}
    assert matched_models == {"iMIX / iMIX-S+"}, (
        f"trademark boilerplate caused false matches: {matched_models}"
    )


def test_revision_and_doc_number_extraction():
    doc = _doc("Ultra NX\nInstallation & Operating Guide\n58039.0002 D 04/26\nRev. 2.08B")
    meta = extract_metadata("ultra-nx.pdf", doc)
    assert meta.doc_number == "58039.0002 D"
    assert meta.revision == "2.08B"
