"""Heuristic metadata extraction: manufacturer, machine model(s), document type,
title, revision, and doc number — derived from filename + the first couple of
pages of extracted text.

This is intentionally a data-driven catalog, seeded from manually reviewing page-1
text of every file in the initial ZIP (see data/reports/zip_text_census.csv). It
will misclassify occasional edge cases — that's expected and is exactly why the
plan requires an admin metadata-correction UI (see app/api/routes_admin.py) rather
than trusting this blindly. Every detection here is auditable: it's not an LLM
guess, it's a regex/keyword match, and low/zero-confidence results are surfaced as
'unknown' rather than a wrong guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ingestion.extracted import ExtractedDocument


@dataclass
class MachineSpec:
    manufacturer: str
    model_name: str
    family: str
    machine_type: str
    patterns: list[str]  # regex patterns (case-insensitive) matched against combined text


@dataclass
class MachineMatch:
    manufacturer: str
    model_name: str
    family: str
    machine_type: str
    confidence: float


@dataclass
class DocMetadata:
    manufacturer: str | None
    doc_type: str
    title: str
    revision: str | None
    doc_number: str | None
    machine_matches: list[MachineMatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


MANUFACTURER_MARKERS: list[tuple[str, list[str]]] = [
    ("CMA Dishmachines", [r"cmadishmachines\.com", r"\bC\s?M\s?A\s+D\s?I\s?S\s?H\s?M\s?A\s?C\s?H\s?I\s?N\s?E\s?S\b", r"\bCMA[- ]\d{3}"]),
    ("American Dish Service", [r"American Dish Service", r"\bADS\b.{0,20}(DISHWASHER|GLASSWASHER)"]),
    ("Bunn-O-Matic Corporation", [r"Bunn-?O-?Matic", r"\bBUNN\b", r"bunn\.com"]),
    ("Nuova Simonelli", [r"Nuova (Distribution|Simonelli)", r"nuovadistribution\.com"]),
    ("Grindmaster-Cecilware", [r"Grindmaster-Cecilware", r"gmcw\.com"]),
    ("Dema Engineering Company", [r"DEMA (Engineering|TITAN)", r"\bDEMA\b"]),
]

DOC_TYPE_MARKERS: list[tuple[str, list[str]]] = [
    ("service_repair", [r"SERVICE\s*&?\s*REPAIR MANUAL", r"SERVICE MANUAL\b"]),
    ("parts", [r"PARTS MANUAL", r"PARTS CATALOG"]),
    ("programming", [r"PROGRAMMING MANUAL"]),
    ("installation_operating", [
        r"INSTALLATION\s*(&|AND)?\s*OPERAT(ING|ION)",
        r"INSTALLATION INSTRUCTIONS",
        r"OWNER.?S MANUAL",
        r"INSTALL(ATION)? (&|AND) OPERATION MANUAL",
    ]),
    ("use_and_care", [r"USE AND CARE", r"USER HANDBOOK", r"INSTRUCTION MANUAL", r"LIBRETTO ISTRUZIONI"]),
    ("spec_sheet", [r"SPEC SHEET", r"SPECIFICATION SHEET"]),
    ("training", [r"TRAINING\b"]),
    ("brochure", [r"BROCHURE"]),
]

REVISION_PATTERNS = [
    r"Rev(?:ision)?\.?\s*([0-9]+\.[0-9]+[A-Za-z]?)",
    r"\bED\s?([0-9]+)\b",
]

DOC_NUMBER_PATTERNS = [
    r"\b\d{5}\.\d{4}\s?[A-Z]?\b",              # Bunn: 58039.0002 D
    r"\b07610-\d{3}-\d{2}-\d{2}(?:-[A-Z])?\b",  # CMA wall-guide numbering
    r"\bI-\d{4}\b",                             # Dema: I-1024
]

# Manually curated from data/reports/zip_text_census.csv page-1/2 excerpts.
MACHINE_CATALOG: list[MachineSpec] = [
    # --- CMA Dishmachines ---
    MachineSpec("CMA Dishmachines", "CMA-180/180 Tall", "180 Series", "dishmachine", [r"CMA-180\b", r"180\s?UC", r"180/180 TALL"]),
    MachineSpec("CMA Dishmachines", "CMA-180UC-3", "180 Series", "dishmachine", [r"CMA-180UC-3", r"MODEL C\s*MA-180UC-3"]),
    MachineSpec("CMA Dishmachines", "CMA-181VL", "180 Series", "dishmachine", [r"CMA-181VL", r"181-?VL"]),
    MachineSpec("CMA Dishmachines", "EST-44", "EST Series", "dishmachine", [r"\bEST-?44\b"]),
    MachineSpec("CMA Dishmachines", "EST-66", "EST Series", "dishmachine", [r"\bEST-?66\b"]),
    MachineSpec("CMA Dishmachines", "EAH/EC/3-Door", "EAH/EC Series", "dishmachine", [r"EAH/EC/3-Door", r"\bEAH\b.{0,10}\bEC\b"]),
    MachineSpec("CMA Dishmachines", "AH/B/C/Scullery/Pizza/Bowl", "AH Series", "dishmachine", [r"AH/B/C/Scullery/Pizza/Bowl"]),
    MachineSpec("CMA Dishmachines", "AJ/AJX Series", "AJ Series", "conveyor dishmachine", [r"\bAJ[- ]?AJX\b", r"AJ SERIES CONVEYOR"]),
    MachineSpec("CMA Dishmachines", "Conserver XL2", "Conserver Series", "conveyor dishmachine", [r"CONSERVER(\s*®)?\s*XL2", r"Cons-?XL2"]),
    MachineSpec("CMA Dishmachines", "Delta 115/1200", "Delta Series", "glasswasher dishmachine", [r"DELTA\s*115[/-]1200"]),

    # --- American Dish Service ---
    MachineSpec("American Dish Service", "ADC-44", "Conveyor", "dishwasher", [r"ADC-?44"]),
    MachineSpec("American Dish Service", "AFB/AFB-C", "Upright", "dishwasher", [r"\bAFB(-C)?\b"]),
    MachineSpec("American Dish Service", "AF-3D-S/AFC-3D-S", "Upright", "dishwasher", [r"AF-?3D-?S", r"AFC-?3D-?S", r"AF-3DS"]),
    MachineSpec("American Dish Service", "ASQ II", "ASQ Series", "glasswasher", [r"\bASQ\s?(II|2)\b", r"\bASQ2\b"]),
    MachineSpec("American Dish Service", "ASQ Glasswasher", "ASQ Series", "glasswasher", [r"\bASQ Glasswasher\b"]),
    MachineSpec("American Dish Service", "ASQ16", "ASQ Series", "glasswasher", [r"\bASQ\s?-?16\b"]),
    MachineSpec("American Dish Service", "ETAF-3/ETAF-M", "PDH Series", "undercounter dishwasher", [r"ETAF-?3", r"ETAF-?M"]),

    # --- Bunn-O-Matic ---
    MachineSpec("Bunn-O-Matic Corporation", "Axiom", "Axiom Series", "coffee brewer", [r"\bAXIOM\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "TF DBC w/ Smart Funnel (Single/Dual)", "DBC Series", "coffee brewer", [r"TF DBC", r"Smart Funnel"]),
    MachineSpec("Bunn-O-Matic Corporation", "ICB / Twin ITCB Combo", "Infusion Series", "coffee brewer", [r"\bICB\b.{0,15}ITCB", r"ICB & Twin ITCB"]),
    MachineSpec("Bunn-O-Matic Corporation", "ICBA/ICBB/ICBC/ICB-DV/ICB Twin", "Infusion Series", "coffee brewer", [r"\bICBA\b", r"\bICBB\b", r"\bICBC\b", r"ICB-DV", r"ICB Twin"]),
    MachineSpec("Bunn-O-Matic Corporation", "ITB/ITCB/ITCB-DV HV/ITCB TWIN HV", "Infusion Series", "coffee brewer", [r"\bITB\b", r"\bITCB\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "IMIX-3/4/5", "iMIX Series", "frozen/blended beverage machine", [r"\bI[- ]?MIX-?[345]\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "iMIX / iMIX-S+", "iMIX Series", "frozen/blended beverage machine", [r"\biMIX-?S\+"]),
    MachineSpec("Bunn-O-Matic Corporation", "TB3/TB6 Tea Brewers", "TB Series", "tea brewer", [r"\bTB3\b", r"\bTB6\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "VPR-VPS Series", "VPR-VPS Series", "coffee brewer", [r"\bVPR-?VPS\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "JDF-2S/JDF-4S", "Silver Series", "juice dispenser", [r"\bJDF-?2S\b", r"\bJDF-?4S\b", r"JDF 2_4", r"JDF-2\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "C/CS/CT/CWTF/CRT/CRTF Series", "C Series", "coffee brewer", [r"\bCWTF\b", r"\bCRTF\b", r"C, CS, CT, CWTF, CRT, CRTF"]),
    MachineSpec("Bunn-O-Matic Corporation", "Grinders (MHG/FPG/G1/G2/G3/G9/trifecta)", "Grinder Series", "coffee grinder", [r"\bMHG\b", r"\bG9-2T DBC\b", r"G2 trifecta", r"\bFPG-?2?\b", r"\bG1\b.{0,10}\bG2\b.{0,10}\bG3\b"]),
    MachineSpec("Bunn-O-Matic Corporation", "Ultra NX", "Ultra Series", "frozen beverage dispenser", [r"Ultra\s?®?\s?NX"]),
    MachineSpec("Bunn-O-Matic Corporation", "Ultra-1/Ultra-2", "Ultra Series", "frozen beverage dispenser", [r"ULTRA-1", r"ULTRTA-2", r"ULTRA-2"]),

    # --- Nuova Simonelli ---
    MachineSpec("Nuova Simonelli", "Oscar II", "Oscar Series", "espresso machine", [r"Oscar\s?II"]),
    MachineSpec("Nuova Simonelli", "G60", "Grinder", "espresso grinder", [r"\bG60\b"]),
    MachineSpec("Nuova Simonelli", "Appia II", "Appia Series", "espresso machine", [r"Appia\s?II"]),
    MachineSpec("Nuova Simonelli", "Appia Life", "Appia Series", "espresso machine", [r"Appia Life"]),
    MachineSpec("Nuova Simonelli", "Compact", "Compact Series", "espresso machine", [r"\bCOMPACT\b"]),

    # --- Grindmaster-Cecilware ---
    MachineSpec("Grindmaster-Cecilware", "GB Powdered Beverage Dispenser", "GB Series", "beverage dispenser", [r"\bGB\d?M?10?-LD\b", r"Powdered Beverage Dispenser"]),
    MachineSpec("Grindmaster-Cecilware", "FE/CL Coffee Urns", "Coffee Urn Series", "coffee urn", [r"FE75N|FE100N|FE200|FE300|CL75N|CL100N|CL200"]),

    # --- Dema Engineering ---
    MachineSpec("Dema Engineering Company", "Titan II Warewash Control (T.812/T.813)", "Titan Series", "chemical dispensing control", [r"TITAN.{0,10}(II|2)", r"T\.812", r"T\.813"]),
]


_TRADEMARK_MARKER_RE = re.compile(r"trademarks?\s+(of|or)\b|registered trademark", re.IGNORECASE)


def _strip_trademark_boilerplate(text: str) -> str:
    """Manufacturers (Bunn especially) print a blanket legal notice listing
    every product-line name they own as a trademark, e.g.: '...AutoPOD, AXIOM,
    ... Smart Funnel, ... Ultra are either trademarks or registered trademarks
    of Bunn-O-Matic Corporation.' That sentence alone made every Bunn manual
    falsely match Axiom, Ultra, TF DBC, etc. regardless of what machine the
    manual is actually about — a direct threat to the no-cross-model-attribution
    requirement. Fix: strip the dense comma-separated run leading up to any
    'trademark(s) of' marker before machine-pattern matching runs.
    Over-stripping a little surrounding legal text is harmless (none of it
    matches a machine or doc-type pattern anyway); under-stripping is the
    actual bug, so the backward window is generous."""
    lines = text.split("\n")
    drop = [False] * len(lines)
    for i, line in enumerate(lines):
        if not _TRADEMARK_MARKER_RE.search(line):
            continue
        drop[i] = True
        j = i - 1
        window_left = 40
        while j >= 0 and window_left > 0 and lines[j].count(",") >= 2:
            drop[j] = True
            j -= 1
            window_left -= 1
    return "\n".join(l for l, d in zip(lines, drop) if not d)


def _search_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _first_match(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


def _guess_title(filename: str, extracted: ExtractedDocument) -> str:
    if extracted.pages:
        first_page = extracted.pages[0]
        rank0 = [t for t, rank in first_page.headings if rank == 0]
        if rank0:
            return rank0[0][:200]
        first_line = next((ln.strip() for ln in first_page.text.splitlines() if ln.strip()), None)
        if first_line:
            return first_line[:200]
    stem = filename.rsplit(".", 1)[0]
    return stem


def extract_metadata(filename: str, extracted: ExtractedDocument) -> DocMetadata:
    page_text = "\n".join(p.text for p in extracted.pages[:3])
    page_text = _strip_trademark_boilerplate(page_text)
    combined = f"{filename}\n{page_text}"

    manufacturer = None
    for name, patterns in MANUFACTURER_MARKERS:
        if _search_any(patterns, combined):
            manufacturer = name
            break

    doc_type = "unknown"
    for dtype, patterns in DOC_TYPE_MARKERS:
        if _search_any(patterns, combined):
            doc_type = dtype
            break

    revision = _first_match(REVISION_PATTERNS, combined)
    doc_number = _first_match(DOC_NUMBER_PATTERNS, combined)
    title = _guess_title(filename, extracted)

    matches: list[MachineMatch] = []
    for spec in MACHINE_CATALOG:
        if manufacturer and spec.manufacturer != manufacturer:
            continue
        if _search_any(spec.patterns, combined):
            confidence = 0.9 if manufacturer else 0.6
            matches.append(
                MachineMatch(
                    manufacturer=spec.manufacturer,
                    model_name=spec.model_name,
                    family=spec.family,
                    machine_type=spec.machine_type,
                    confidence=confidence,
                )
            )

    notes = []
    if manufacturer is None:
        notes.append("Manufacturer not confidently detected; needs admin review.")
    if not matches:
        notes.append("No machine model matched; needs admin review and catalog update.")
    if doc_type == "unknown":
        notes.append("Document type not confidently detected from title-page keywords.")

    return DocMetadata(
        manufacturer=manufacturer,
        doc_type=doc_type,
        title=title,
        revision=revision,
        doc_number=doc_number,
        machine_matches=matches,
        notes=notes,
    )
