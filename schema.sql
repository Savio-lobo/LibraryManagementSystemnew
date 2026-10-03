-- =========================================================
-- Library Management System - Database Schema (SQLite)
-- =========================================================
--
-- This file documents the exact relational schema the application
-- creates and maintains at startup (see init_database() and
-- _ensure_borrow_record_schema() in app.py). It is safe to run
-- against a fresh database file - every statement is idempotent
-- (CREATE TABLE IF NOT EXISTS) and matches what the app itself
-- executes on first run, so you never need to run this file by
-- hand for a normal installation. It's provided so the schema is
-- visible and reviewable on its own, separate from the Python code.
--
-- Tables, in dependency order:
--   users             - accounts (borrowers, librarians, admins)
--   books             - the catalog
--   borrow_records    - one row per borrowing transaction
--   renewal_requests  - one row per renewal request against a
--                       borrowing transaction, reviewed by a
--                       librarian/admin
--   system_settings   - tunable business-rule values (borrowing
--                       period, renewal period) read by the backend
--                       instead of being hardcoded
--   waitlist          - users waiting for a book with 0 copies free
--   notifications     - in-app notifications shown to a user
--   password_reset_otps - Forgot Password email OTP requests (OTP is
--                       stored only as a bcrypt hash)
-- =========================================================


-- ---------------------------------------------------------
-- USERS
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    email    TEXT PRIMARY KEY,        -- login identity
    name     TEXT,
    password TEXT,                    -- bcrypt hash, never plaintext
    role     TEXT                     -- 'user' | 'librarian' | 'admin'
);


-- ---------------------------------------------------------
-- BOOKS
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
    book_id   TEXT PRIMARY KEY,
    book_name TEXT,
    author    TEXT,
    publisher TEXT,
    isbn      TEXT,
    category  TEXT,
    copies    INTEGER                 -- copies currently available
);


-- ---------------------------------------------------------
-- BORROW RECORDS
--
-- One row per borrowing transaction. book_id/user_email are the
-- foreign-key-style links back to books/users (see note below on
-- why they aren't declared as SQLite FOREIGN KEY constraints on
-- this particular table).
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS borrow_records (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email             TEXT,      -- -> users.email
    book_id                TEXT,      -- -> books.book_id
    book_name              TEXT,      -- denormalized snapshot at
    author                 TEXT,      -- borrow time, kept so a later
    category               TEXT,      -- edit/delete of the book does
                                       -- not corrupt historical records
    borrowed_date          TEXT,
    due_date               TEXT,      -- calculated by the backend from
                                       -- system_settings.BORROW_DURATION_DAYS
    returned               INTEGER,   -- 0/1
    returned_date          TEXT,
    return_requested       INTEGER,   -- 0/1
    return_requested_date  TEXT,
    book_condition         TEXT,
    days_late              INTEGER,
    late_fine              INTEGER,
    damage_fine            INTEGER,
    lost_fine              INTEGER,
    total_fine             INTEGER,
    fine_status            TEXT,
    fine_paid_date         TEXT,
    renewal_count          INTEGER NOT NULL DEFAULT 0,
    last_renewed_at        TEXT
);


-- ---------------------------------------------------------
-- RENEWAL REQUESTS
--
-- A real transaction/workflow table: a user's "Request Renewal"
-- click inserts a PENDING row here. It is NEVER used to change
-- due_date directly - only a librarian/admin approval
-- (status -> APPROVED) triggers the actual due_date update on
-- borrow_records, and only after the backend re-validates the
-- request server-side.
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS renewal_requests (
    renewal_request_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    borrow_id           INTEGER NOT NULL,      -- -> borrow_records.id
    user_email          TEXT NOT NULL,         -- -> users.email
    book_id             TEXT,                  -- -> books.book_id
    requested_at        TEXT NOT NULL,
    requested_due_date  TEXT,                  -- preview of the due
                                                -- date if approved
    status              TEXT NOT NULL DEFAULT 'PENDING',
                                                -- 'PENDING' | 'APPROVED' | 'REJECTED'
    librarian_email     TEXT,                  -- -> users.email (reviewer)
    reviewed_at         TEXT,
    remarks             TEXT,
    FOREIGN KEY (borrow_id)   REFERENCES borrow_records(id),
    FOREIGN KEY (user_email)  REFERENCES users(email),
    FOREIGN KEY (book_id)     REFERENCES books(book_id)
);


-- ---------------------------------------------------------
-- SYSTEM SETTINGS
--
-- The single source of truth for tunable business rules. The
-- backend reads these at request time (get_borrow_days() /
-- get_renewal_days() in app.py) rather than hardcoding the numbers,
-- and the frontend never calculates or stores a due date itself -
-- it only displays what the backend returns.
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    setting_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    setting_name  TEXT UNIQUE NOT NULL,
    setting_value TEXT NOT NULL
);

INSERT OR IGNORE INTO system_settings (setting_name, setting_value)
VALUES ('BORROW_DURATION_DAYS', '14');

INSERT OR IGNORE INTO system_settings (setting_name, setting_value)
VALUES ('RENEWAL_DURATION_DAYS', '7');


-- ---------------------------------------------------------
-- WAITLIST (pre-existing, unchanged)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS waitlist (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email     TEXT,
    user_name      TEXT,
    book_id        TEXT,
    book_name      TEXT,
    requested_date TEXT
);


-- ---------------------------------------------------------
-- NOTIFICATIONS (pre-existing, unchanged)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY,
    user_email     TEXT,
    message        TEXT,
    type           TEXT,
    created_date   TEXT,
    read           INTEGER,
    title          TEXT,
    book_name      TEXT,
    fine_amount    INTEGER,
    days_late      INTEGER,
    book_condition TEXT,
    reason         TEXT
);


-- ---------------------------------------------------------
-- PASSWORD RESET OTPS (Forgot Password -> Email OTP flow)
--
-- One row per OTP request. otp_hash is a bcrypt hash and is blanked
-- as soon as the OTP is verified, used up or superseded; the OTP
-- itself is never stored. No foreign keys, so it cannot interfere
-- with the users table. Created by _ensure_password_reset_schema().
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS password_reset_otps (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT NOT NULL,
    otp_hash         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,   -- created_at + 5 minutes
    attempts         INTEGER NOT NULL DEFAULT 0,
    verified         INTEGER NOT NULL DEFAULT 0,
    used             INTEGER NOT NULL DEFAULT 0,
    reset_expires_at TEXT             -- set on verification (+10 min)
);


-- =========================================================
-- Note on foreign keys on `users`/`books`/`borrow_records`
-- =========================================================
-- These three tables predate this change and already hold live
-- production data; SQLite cannot add a FOREIGN KEY constraint to an
-- existing table without recreating it, which risks existing data
-- and was avoided per the "do not touch working features" brief.
-- The new renewal_requests table (and any future table) declares
-- real FOREIGN KEY constraints as shown above, and the app always
-- enables enforcement at connection time:
--
--     PRAGMA foreign_keys = ON;
--
-- (see get_db_connection() in app.py).
