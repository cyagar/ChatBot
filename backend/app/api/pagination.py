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

from fastapi import HTTPException, Response, status

NEXT_CURSOR_HEADER = "X-Next-Cursor"
HAS_MORE_HEADER = "X-Has-More"


def encode_cursor(*parts) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(parts)).encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> list:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid pagination cursor.") from None


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
