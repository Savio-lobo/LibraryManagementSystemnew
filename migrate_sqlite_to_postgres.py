#!/usr/bin/env python3
"""
One-time migration: copy ALL data from the old SQLite file (library.db)
into the central PostgreSQL database named by DATABASE_URL.

    python migrate_sqlite_to_postgres.py                 # migrate + verify
    python migrate_sqlite_to_postgres.py --dry-run       # read & report only
    python migrate_sqlite_to_postgres.py --sqlite PATH   # other source file

Safety:
  * The SQLite file is opened READ-ONLY and its SHA-256 is checked before and
    after - it is never modified.
  * Everything is loaded in ONE transaction: it all succeeds or nothing is
    written.
  * Original ids are kept (borrow record #7 stays #7, ...). Id counters are
    advanced so new rows continue after the old ones.
  * If the cloud database already contains data it REFUSES to continue. Use
    --replace only for a database that holds nothing you want to keep (e.g.
    the app was started once before migrating and created the two default
    accounts); you will be asked to type REPLACE.
  * After loading, every table is re-read from BOTH databases and compared
    value-for-value.

Run this BEFORE the deployed app is used for the first time.
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE = os.path.join(HERE, "backup_original", "library.db")
SCHEMA_FILE = os.path.join(HERE, "schema_postgres.sql")

# name, columns (insertion/compare order), order-by, identity column
TABLES = [
    ("users", ["email", "name", "password", "role"], "rowid", None),
    ("books", ["book_id", "book_name", "author", "publisher", "isbn",
               "category", "copies"], "rowid", None),
    ("borrow_records", [
        "id", "user_email", "book_id", "book_name", "author", "category",
        "borrowed_date", "due_date", "returned", "returned_date",
        "return_requested", "return_requested_date", "book_condition",
        "days_late", "late_fine", "damage_fine", "lost_fine", "total_fine",
        "fine_status", "fine_paid_date", "renewal_count", "last_renewed_at",
    ], "id", "id"),
    ("renewal_requests", [
        "renewal_request_id", "borrow_id", "user_email", "book_id",
        "requested_at", "requested_due_date", "status", "librarian_email",
        "reviewed_at", "remarks",
    ], "renewal_request_id", "renewal_request_id"),
    ("system_settings", ["setting_id", "setting_name", "setting_value"],
     "setting_id", "setting_id"),
    ("waitlist", ["id", "user_email", "user_name", "book_id", "book_name",
                  "requested_date"], "id", "id"),
    ("notifications", [
        "id", "user_email", "message", "type", "created_date", "read",
        "title", "book_name", "fine_amount", "days_late", "book_condition",
        "reason",
    ], "id", "id"),
    ("password_reset_otps", [
        "id", "email", "otp_hash", "created_at", "expires_at", "attempts",
        "verified", "used", "reset_expires_at",
    ], "id", "id"),
]


def qi(name):
    return '"' + name + '"'


def load_dotenv():
    for fname in (".env", ".env.txt"):
        path = os.path.join(HERE, fname)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and not os.environ.get(k, "").strip():
                    os.environ[k] = v


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_sqlite_readonly(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                           uri=True)


def read_source(sconn):
    data = {}
    for name, cols, order, _ in TABLES:
        exists = sconn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        if not exists:
            data[name] = []
            continue
        sql = "SELECT %s FROM %s ORDER BY %s" % (
            ", ".join(qi(c) for c in cols), qi(name), order)
        data[name] = [tuple(r) for r in sconn.execute(sql).fetchall()]
    return data


def source_sequences(sconn):
    try:
        return dict(sconn.execute(
            "SELECT name, seq FROM sqlite_sequence").fetchall())
    except sqlite3.Error:
        return {}


def run_schema(cur):
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        text = re.sub(r"--[^\n]*", "", f.read())
    for stmt in [s.strip() for s in text.split(";") if s.strip()]:
        cur.execute(stmt)


def target_counts(cur):
    out = {}
    for name, *_ in TABLES:
        cur.execute("SELECT COUNT(*) FROM %s" % qi(name))
        out[name] = cur.fetchone()[0]
    return out


def verify(sconn, pconn, source_hash_before=None, sqlite_path=None):
    """Compare every table value-for-value. Returns list of problems."""
    problems = []
    src = read_source(sconn)
    seqs = source_sequences(sconn)
    cur = pconn.cursor()
    print("\nVerification (SQLite source vs PostgreSQL target)")
    print("-" * 64)
    for name, cols, order, ident in TABLES:
        pg_order = "seq" if name in ("users", "books") else order
        cur.execute("SELECT %s FROM %s ORDER BY %s" % (
            ", ".join(qi(c) for c in cols), qi(name), pg_order))
        dst = [tuple(r) for r in cur.fetchall()]
        ok = src[name] == dst
        print("%-22s source=%-4d target=%-4d %s" % (
            name, len(src[name]), len(dst), "IDENTICAL" if ok else "MISMATCH"))
        if not ok:
            problems.append("%s: data differs" % name)
            for i, (a, b) in enumerate(zip(src[name], dst)):
                if a != b:
                    problems.append("  first difference at row %d:\n    "
                                    "source=%r\n    target=%r" % (i, a, b))
                    break
        if ident:
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)",
                        (name, ident))
            seq_name = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(MAX(%s), 0) FROM %s" % (
                qi(ident), qi(name)))
            mx = cur.fetchone()[0]
            expected_min = max(mx, seqs.get(name, 0))
            cur.execute("SELECT last_value, is_called FROM %s" % seq_name)
            last, called = cur.fetchone()
            nxt = last + 1 if called else last
            if nxt <= expected_min and (expected_min > 0):
                problems.append("%s: next id would be %d but ids up to %d "
                                "already exist" % (name, nxt, expected_min))
    # relationships
    checks = {
        "renewal_requests -> borrow_records": (
            "SELECT COUNT(*) FROM renewal_requests r LEFT JOIN "
            "borrow_records b ON b.id = r.borrow_id WHERE b.id IS NULL"),
        "renewal_requests -> users": (
            "SELECT COUNT(*) FROM renewal_requests r LEFT JOIN users u "
            "ON u.email = r.user_email WHERE u.email IS NULL"),
    }
    for label, sql in checks.items():
        cur.execute(sql)
        bad = cur.fetchone()[0]
        print("%-36s broken links: %d" % (label, bad))
        if bad:
            problems.append("%s: %d broken links" % (label, bad))
    cur.execute("SELECT email, role FROM users WHERE role IN "
                "('admin','librarian') ORDER BY role")
    print("Staff accounts in cloud DB:", [tuple(r) for r in cur.fetchall()])
    if sqlite_path and source_hash_before:
        same = sha256(sqlite_path) == source_hash_before
        print("SQLite source file unchanged: %s" % ("yes" if same else "NO"))
        if not same:
            problems.append("SQLite source file changed during run!")
    print("-" * 64)
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="wipe the cloud tables first (asks you to confirm)")
    args = ap.parse_args()

    load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL is not set (environment variable or .env).")
    if not os.path.isfile(args.sqlite):
        sys.exit("SQLite file not found: %s" % args.sqlite)

    before = sha256(args.sqlite)
    print("Source:", args.sqlite)
    print("Source SHA-256:", before)
    sconn = open_sqlite_readonly(args.sqlite)
    data = read_source(sconn)
    seqs = source_sequences(sconn)
    print("\nRows found in SQLite:")
    for name, *_ in TABLES:
        print("  %-22s %d" % (name, len(data[name])))
    if args.dry_run:
        print("\nDry run - nothing written.")
        return

    with psycopg.connect(url, connect_timeout=30) as pconn:
        cur = pconn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(727274)")
        run_schema(cur)

        counts = target_counts(cur)
        nonempty = {k: v for k, v in counts.items()
                    if v and k != "system_settings"}
        if nonempty:
            print("\nThe cloud database is NOT empty:", nonempty)
            if not args.replace:
                pconn.rollback()
                sys.exit(
                    "Refusing to continue so nothing is overwritten.\n"
                    "If this database holds nothing you need (for example "
                    "the app was started once\nbefore migrating and created "
                    "its two default accounts), re-run with --replace.")
            if input("Type REPLACE to delete the current cloud data and "
                     "load the SQLite data: ").strip() != "REPLACE":
                pconn.rollback()
                sys.exit("Cancelled. Nothing changed.")
        # clear (also clears the 2 default settings the schema just seeded)
        cur.execute("TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(
            qi(t[0]) for t in TABLES))

        for name, cols, order, ident in TABLES:
            rows = data[name]
            if not rows:
                continue
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                qi(name), ", ".join(qi(c) for c in cols),
                ", ".join(["%s"] * len(cols)))
            cur.executemany(sql, rows)

        for name, cols, order, ident in TABLES:
            if not ident:
                continue
            cur.execute("SELECT COALESCE(MAX(%s), 0) FROM %s" % (
                qi(ident), qi(name)))
            top = max(cur.fetchone()[0], seqs.get(name, 0))
            if top > 0:
                cur.execute("SELECT setval(pg_get_serial_sequence(%s, %s), "
                            "%s, true)", (name, ident, top))
        pconn.commit()
        print("\nData loaded and committed.")

        problems = verify(sconn, pconn, before, args.sqlite)

    if problems:
        print("\nPROBLEMS FOUND:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("\nSUCCESS: the cloud database contains exactly the data that was "
          "in library.db.")


if __name__ == "__main__":
    main()
