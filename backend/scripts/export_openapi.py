"""CLI: regenerate backend/openapi.json from the live FastAPI app.

Committing a snapshot of the real, generated schema to git makes the API
contract something a reviewer can read and diff, rather than only reachable
by starting the server and hitting /openapi.json.
tests/unit/test_openapi_contract.py fails if this file goes stale (an
endpoint changed without re-running this script) -- run it, and this script,
whenever a route's request/response shape changes.

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
