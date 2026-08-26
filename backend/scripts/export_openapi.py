"""CLI: regenerate backend/openapi.json from the live FastAPI app.

Phase 1 (narrowed scope, 2026-08-26): "Define ... an OpenAPI contract and
publish it in CI." No CI exists yet in this repo (that's change set 2, not
built), and Phase 1 stays on the existing FastAPI app rather than
bootstrapping a separate service with its own route namespace (see
docs/OWNER_DECISION_GATE.md section 9) -- so "publish" here means "commit a
snapshot of the real, generated schema to git," making it something a
reviewer can read and diff, rather than only reachable by starting the
server and hitting /openapi.json. tests/unit/test_openapi_contract.py
fails if this file goes stale (an endpoint changed without re-running this
script), which is the proportionate stand-in for "CI checks it" until CI
itself exists.

Usage (from backend/):
    py scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def export() -> dict:
    schema = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return schema


def main():
    schema = export()
    print(f"Wrote {OUTPUT_PATH} ({len(schema.get('paths', {}))} paths).")


if __name__ == "__main__":
    main()
