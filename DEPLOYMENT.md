# Moving to one central cloud database (PostgreSQL)

After this change the app has **no SQLite file and no JSON files**. Everything
(users, books, borrow records, renewals, renewal requests, waitlist,
notifications, settings) lives in ONE hosted PostgreSQL database. Every Admin,
Librarian and User of the deployed site reads and writes that same database,
so every valid change is visible to everyone immediately.

## Why PostgreSQL (and why Neon)

Your data is relational (users <-> borrow records <-> books <-> renewal
requests, with foreign keys and multi-step updates such as "issue book =
create record + reduce copies"). PostgreSQL is the natural fit; MongoDB would
have meant redesigning all of that for no benefit. It is also the closest
match to SQLite's SQL, so the app's ~75 queries carried over almost unchanged.

**Recommended host: Neon** (standard PostgreSQL, free plan, no credit card).
Checked against the providers' current docs:

| Provider | Free-tier behaviour that matters for a library |
|---|---|
| **Neon** | 0.5 GB, runs indefinitely. Compute sleeps after 5 min idle and wakes on the next request (the first request after a quiet period is ~1-2 s slower). |
| Supabase | Free projects are **paused after 1 week of inactivity**. |
| Render Postgres | Free database **expires after 30 days and is then deleted**. Avoid. |

Your whole dataset is a few KB, so storage is not a concern. The app works with
any PostgreSQL (Supabase, Railway, ...): only `DATABASE_URL` changes.

## One-time setup

1. **Create the database.** Sign up at neon.com -> new project -> choose the
   region closest to where you will host the app -> **Connect** -> copy the
   **pooled** connection string (`postgresql://...?sslmode=require`).
2. **Configure.** Copy `.env.example` to `.env` and paste the string into
   `DATABASE_URL`. Set `FLASK_SECRET_KEY` and the mail settings too.
3. **Install.** `pip install -r requirements.txt`
4. **Preview the migration (writes nothing):**
   `python migrate_sqlite_to_postgres.py --dry-run`
5. **Migrate your data:** `python migrate_sqlite_to_postgres.py`
   It reads `backup_original/library.db` (read-only), loads everything in one
   all-or-nothing transaction, keeps all original ids, then re-reads both
   databases and compares them value-for-value. You should see `IDENTICAL`
   for every table and `SUCCESS`. You can re-run the comparison any time
   *before go-live* with `python verify_migration.py`.
   **Do this before the app is started against the new database for the first
   time.** If you forgot, the script refuses to overwrite anything; if the
   cloud DB only holds the 2 default accounts the app created, use `--replace`.
6. **Run locally to check:** `python app.py`, log in as Admin/Librarian.

## Deploying

Set these as environment variables on your host (not in git): `DATABASE_URL`,
`FLASK_SECRET_KEY` (same value on every instance), `MAIL_USERNAME`,
`MAIL_PASSWORD`, and `TZ` (your library's timezone, e.g. `Asia/Kolkata`;
servers default to UTC, which would shift due-date/overdue/renewal-window
logic by hours). Start command: `gunicorn app:app` (a `Procfile` is included).
Do **not** run `python app.py` in production.

## How simultaneous users are handled

* One shared connection pool; no per-computer database.
* The old "delete the whole table and re-insert everything" saves were
  replaced by row-level saves (`db.sync_table`): only rows that were added,
  changed or removed are written. Two people editing *different* rows never
  overwrite each other.
* If two people change the *same* row at the same instant (e.g. two members
  borrowing the last copy), the second is refused with a friendly "someone else
  changed this, please retry" message instead of silently corrupting counts.
* Notifications are inserted one row at a time with database-assigned ids.

## Backups

Your originals are untouched in `backup_original/` (SQLite files, JSON archive,
original `app.py`/schema/requirements/zip). Keep that folder private; it holds
password hashes and is git-ignored. Neon's free plan keeps only a short
point-in-time restore window (about 6 hours), so also take a periodic dump:
`pg_dump "$DATABASE_URL" -Fc -f library_backup.dump`.

## Known behaviour carried over unchanged

* `_ensure_required_accounts()` in `app.py` re-applies the Admin/Librarian
  password hashes at every startup (same as before), so a password change made
  to those two accounts inside the app reverts on the next restart.
* Multi-step actions (e.g. issue = save record, then save book copies) are
  separate database transactions, as before.
