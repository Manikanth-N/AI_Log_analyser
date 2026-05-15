#!/usr/bin/env python3
"""Initialize PostgreSQL schema and verify storage directories."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from storage.metadata_db import create_tables, MetadataDB


def main():
    print(f"==> Connecting to DB: {settings.database_url}")
    try:
        create_tables()
        print("    Tables created (or already exist).")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    db = MetadataDB()
    flights = db.list_flights(limit=1)
    print(f"    DB connection OK. Existing flights: {db.list_flights.__doc__ or 'n/a'}")

    for d in [settings.storage_root, settings.raw_storage, settings.flights_storage]:
        d.mkdir(parents=True, exist_ok=True)
        print(f"    Storage dir ready: {d}")

    print("\n==> Database initialization complete.")


if __name__ == "__main__":
    main()
