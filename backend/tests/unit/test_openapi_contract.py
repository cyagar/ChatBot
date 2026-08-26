"""Phase 1 (narrowed scope, 2026-08-26): "Define ... an OpenAPI contract and
publish it in CI." No CI exists yet in this repo -- this test is the
proportionate stand-in until it does: it fails if backend/openapi.json (the
committed, "published" contract -- see scripts/export_openapi.py) has
drifted from what the live app actually generates, e.g. a route's
parameters or response model changed without re-running that script.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import app

OPENAPI_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "openapi.json"


def test_committed_openapi_contract_matches_the_live_app_schema():
    assert OPENAPI_JSON_PATH.is_file(), (
        f"{OPENAPI_JSON_PATH} does not exist -- run `py scripts/export_openapi.py` from backend/."
    )
    committed = json.loads(OPENAPI_JSON_PATH.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert committed == live, (
        "backend/openapi.json is stale -- a route changed without re-running "
        "`py scripts/export_openapi.py` from backend/ to regenerate it."
    )


def test_every_api_route_has_a_tag():
    """Cheap sanity check on contract quality, not just that a file exists --
    an untagged route makes a generated client (or just a human reading
    /docs) worse without failing anything else. Scoped to /api/* -- the
    handful of non-API routes on this app (/, /admin, /healthz, the PWA
    manifest/service-worker) aren't part of the contract this item is
    about."""
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, operation in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            assert operation.get("tags"), f"{method.upper()} {path} has no tag"
