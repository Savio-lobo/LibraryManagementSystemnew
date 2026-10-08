#!/usr/bin/env python3
"""
Re-check, at any time, that the cloud PostgreSQL database still matches the
original library.db snapshot (only meaningful BEFORE people start using the
live site - afterwards the cloud database is expected to differ).

    python verify_migration.py [--sqlite backup_original/library.db]
"""
import argparse
import os
import sys

import psycopg

import migrate_sqlite_to_postgres as m

ap = argparse.ArgumentParser()
ap.add_argument("--sqlite", default=m.DEFAULT_SQLITE)
args = ap.parse_args()

m.load_dotenv()
url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    sys.exit("DATABASE_URL is not set (environment variable or .env).")

before = m.sha256(args.sqlite)
sconn = m.open_sqlite_readonly(args.sqlite)
with psycopg.connect(url, connect_timeout=30) as pconn:
    problems = m.verify(sconn, pconn, before, args.sqlite)
if problems:
    print("\nDIFFERENCES FOUND:")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("OK: cloud database matches library.db exactly.")
