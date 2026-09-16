"""Phase 1 (narrowed scope, 2026-08-26): "Add stable cursor pagination for
machines, history, messages, and saved answers."

Response BODIES stay exactly the plain JSON arrays they always were --
Android's Retrofit interfaces and the web UI's JS both already parse these
endpoints as a flat list, and neither is being changed for this (see
docs/OWNER_DECISION_GATE.md section 9: no Android refactor). Pagination
metadata instead rides on response headers (`X-Next-Cursor`, `X-Has-More`),
GitHub-API-style: a client that doesn't know about them still gets the same
first page it always did; a client that does can page through the rest.

The cursor itself is an opaque, base64-encoded JSON array of the last row's
sort-key values -- never a raw offset, so a row inserted or deleted between
two page fetches can't shift every subsequent page by one (the classic
LIMIT/OFFSET instability this item's "stable" is asking to avoid).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime

from fastapi import HTTPException, Response, status

NEXT_CURSOR_HEADER = "X-Next-Cursor"
HAS_MORE_HEADER = "X-Has-More"


def _json_default(value):
    # A cursor part can be a Postgres TIMESTAMPTZ column's value, which
    # psycopg hands back as a real datetime -- json.dumps doesn't know how
    # to serialize that on its own. The decoded isoformat string round-trips
    # fine as a query parameter (Postgres infers timestamptz from context),
    # so there's no matching decode_cursor-side conversion needed.
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def encode_cursor(*parts) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(list(parts), default=_json_default).encode("utf-8")
    ).decode("ascii")


_CURSOR_SCALAR_TYPES = (str, int, float, bool, type(None))


def decode_cursor(cursor: str, expected_len: int) -> list:
    """P1-10 (independent follow-up review): decoding used to accept any
    valid base64/JSON and hand it straight to the caller's tuple-unpack --
    a well-formed cursor with the wrong shape (too few/many elements, or a
    nested list/dict where a scalar SQL parameter is expected) raised an
    unhandled ValueError/TypeError instead of a clean 400. Every caller now
    declares how many parts it expects and gets a 400 for anything else."""
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid pagination cursor.") from None
    if (
        not isinstance(parts, list)
        or len(parts) != expected_len
        or not all(isinstance(p, _CURSOR_SCALAR_TYPES) for p in parts)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid pagination cursor.")
    return parts


def paginate(rows: list, limit: int, cursor_for_row) -> tuple[list, str | None]:
    """`rows` must have been fetched with `LIMIT limit + 1`. Returns the
    trimmed page and the next cursor (None if this was the last page).
    `cursor_for_row(row)` builds the cursor from the last row kept."""
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(*cursor_for_row(page[-1])) if has_more and page else None
    return page, next_cursor


def set_pagination_headers(response: Response, next_cursor: str | None) -> None:
    response.headers[HAS_MORE_HEADER] = "true" if next_cursor else "false"
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
