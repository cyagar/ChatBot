"""Shared detection for error-code/part-number-shaped tokens, used by
retrieval (query resolution's follow-up heuristic, the extractive
provider's code-token rescue) -- a single definition of what counts as an
identifier on its own versus what needs surrounding context, instead of
each caller tuning its own regex and drifting apart. A leaf module with no
imports of its own, so retrieval and provider code can both depend on it
with no risk of a circular import.

app/providers/base.py has its own, separately-tuned identifier-shape
check for a different job -- spotting a foreign record's identifier
inside an already-grounded claim's text.
"""
from __future__ import annotations

import re

# A letter+digit mix in either order, 2-8 chars (e.g. "E4", "8A12"),
# excluding a bare unit-suffixed measurement ("120v", "8a") or an ordinal
# ("2nd", "7th") -- those are specs and word-position markers, not
# identifiers, and both collide with this shape since a unit abbreviation
# or ordinal suffix is itself made of letters.
_MIXED_SHAPE_RE = re.compile(r"^(?=.*[a-z])(?=.*[0-9])[a-z0-9]{2,8}$")
_UNIT_SUFFIX_RE = re.compile(r"^\d+(v|a|w|f|c|hz|ma|kg|lb|oz|ft|in|mm|cm|psi|hp|rpm)$")
_ORDINAL_RE = re.compile(r"^\d+(st|nd|rd|th)$")

# A letter prefix (1-4 chars) followed by a hyphen and a digit-led group
# (e.g. "E-04", "WS-1234", "P-118") -- the hyphenated counterpart of the
# mixed-shape rule above, which excludes "-" from its own character class.
# Requiring a digit right after the hyphen keeps a plain compound word
# ("TF-DBC", "2-POSITION") out: neither has a letter prefix followed
# directly by a digit.
_LETTER_PREFIX_HYPHEN_RE = re.compile(r"^[a-z]{1,4}-\d[\da-z]*$")

# Three or more hyphen-separated alphanumeric groups, with at least one
# digit somewhere -- a genuine multi-segment part number ("81-118-31",
# "07610-004-42-11-C"), distinct from an ordinary two-group range
# ("2-3", "120-240"), which needs more context (see is_two_group_digit_range)
# to safely treat as an identifier.
_MULTI_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}$")

# A two-group, all-digit token where the groups are asymmetric in length
# -- a short (<=3 digit) first group and a long (>=4 digit) second group,
# e.g. "082-6302", "285-6127". Measured against a real parts catalog: this
# is its dominant two-group part-number shape, distinct from an ordinary
# range/duration/spec, which is symmetric or both groups short
# ("2-3", "14-15", "120-240", "2015-2019", "1000-1500" all fail this
# pattern and stay ambiguous -- see is_two_group_digit_range).
_ASYMMETRIC_TWO_GROUP_RE = re.compile(r"^\d{1,3}-\d{4,}$")

_TWO_GROUP_RANGE_RE = re.compile(r"^\d+-\d+$")


def is_identifier_token(token: str) -> bool:
    """True for a token shaped like an error code or part number on its
    own, with no surrounding context needed. A bare number alone, or a
    two-group digit range NOT matching the asymmetric part-number shape
    above, is never enough by itself -- see is_two_group_digit_range and
    is_bare_digit_token, which callers may allow only with explicit
    error/part-number context (adjacency to a word like "error", "code",
    "part", or a passage's own chunk_type)."""
    if _MULTI_SEGMENT_RE.match(token) and any(c.isdigit() for c in token):
        return True
    if _ASYMMETRIC_TWO_GROUP_RE.match(token):
        return True
    t = token.lower()
    if _ORDINAL_RE.match(t):
        return False
    if _LETTER_PREFIX_HYPHEN_RE.match(t):
        return True
    return bool(_MIXED_SHAPE_RE.match(t) and not _UNIT_SUFFIX_RE.match(t))


def is_two_group_digit_range(token: str) -> bool:
    """A two-group, all-digit hyphenated token ("2-3", "120-240") --
    shaped like either a short part number or an ordinary range, duration,
    or spec. Ambiguous on its own."""
    return bool(_TWO_GROUP_RANGE_RE.match(token))


def is_bare_digit_token(token: str) -> bool:
    """A token that's all digits, no letters, no hyphen ("10", "120",
    "2026"). Collides constantly with ordinary quantities (a duration, a
    voltage, a step number) -- never an identifier by itself."""
    return token.isdigit()
