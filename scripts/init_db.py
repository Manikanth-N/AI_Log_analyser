#!/usr/bin/env python3
"""
Run Alembic migrations and verify DB + storage are reachable.

Usage (from project root):
  python scripts/init_db.py            # run alembic upgrade head
  python scripts/init_db.py --stamp    # stamp existing pre-Alembic DB at initial revision

For existing production DBs migrated from the AWS deployment (which used
create_all() without Alembic), run --stamp first, then upgrade:

  python scripts/init_db.py --stamp
  python scripts/init_db.py
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from storage.metadata_db import MetadataDB

INITIAL_REVISION = "3a1f8c902d44"


def run_alembic(*args: str) -> None:
    cmd = ["alembic"] + list(args)
    print(f"==> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    if result.returncode != 0:
        print(f"ERROR: alembic command failed (exit {result.returncode})")
        sys.exit(1)


def verify_db() -> None:
    db = MetadataDB()
    try:
        db.list_flights(limit=1)
        print("    DB connection OK.")
    except Exception as e:
        print(f"ERROR: DB verification failed: {e}")
        sys.exit(1)


def main() -> None:
    stamp_mode = "--stamp" in sys.argv

    if stamp_mode:
        print(f"==> Stamping existing DB at initial revision ({INITIAL_REVISION})")
        print("    This marks the DB as at the pre-Phase-6 baseline WITHOUT running DDL.")
        print("    Only use this on a DB that was created by the old create_all() path.")
        run_alembic("stamp", INITIAL_REVISION)
        print("    Done. Run without --stamp to apply pending migrations.")
        return

    run_alembic("upgrade", "head")
    verify_db()

    if not settings.gcs_data_bucket:
        for d in [settings.storage_root, settings.raw_storage, settings.flights_storage]:
            d.mkdir(parents=True, exist_ok=True)
            print(f"    Storage dir ready: {d}")

    print("\n==> Database initialization complete.")


if __name__ == "__main__":
    main()
