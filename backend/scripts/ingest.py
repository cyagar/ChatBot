"""CLI entrypoint: run migrations, then ingest everything from the configured source.

Usage (from backend/):
    py scripts/ingest.py            # ingest + embed
    py scripts/ingest.py --no-embed # parse/index only, skip embedding pass
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import run_migrations
from app.ingestion.pipeline import ingest_all


def main():
    embed = "--no-embed" not in sys.argv

    applied = run_migrations()
    if applied:
        print(f"Applied migrations: {applied}")

    start = time.time()
    report = ingest_all(embed=embed)
    elapsed = time.time() - start

    print(f"\nIngestion run {report.run_id} finished in {elapsed:.1f}s")
    print("Status counts:")
    for status, count in sorted(report.counts().items()):
        print(f"  {status:20} {count}")
    print(f"  {'TOTAL':20} {len(report.outcomes)}")

    if report.near_duplicate_scores:
        top = sorted(report.near_duplicate_scores, key=lambda x: x[2], reverse=True)[:10]
        print("\nTop content-similarity scores observed (near-duplicate calibration):")
        for filename, doc_id, score in top:
            print(f"  {score:.3f}  {filename[:60]}  vs doc {doc_id}")


if __name__ == "__main__":
    main()
