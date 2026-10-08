from flask import Flask, render_template, request, redirect, session, flash, url_for, jsonify
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from datetime import datetime, timedelta
import os
import random
import re
import secrets
import hashlib
import smtplib

import db

app = Flask(__name__)

# =========================================================
# BASE DIRECTORY
#
# Anchors file paths (.env, schema_postgres.sql) to this file's own
# location, so the app works no matter which folder it is launched from.
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# =========================================================
# .env LOADER (no extra dependency)
#
# The project ships a .env.example that says "copy to .env", but
# nothing ever read a .env file - so MAIL_USERNAME / MAIL_PASSWORD
# stayed at their placeholder values and no OTP email could be sent.
# This reads KEY=VALUE lines from a .env file next to app.py. Real
# environment variables always win over the file.
# =========================================================

def _load_env_file(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # An env var that exists but is empty must not block the .env value.
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value


_load_env_file(os.path.join(BASE_DIR, ".env"))
# Windows hides extensions, so a file created as ".env" is often really
# ".env.txt". Accept that too.
_load_env_file(os.path.join(BASE_DIR, ".env.txt"))

# =========================================================
# APP CONFIGURATION
# =========================================================

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    # Development-only fallback so the app still runs if the env var
    # isn't set. Never rely on this in production - set
    # FLASK_SECRET_KEY in the environment instead.
    "dev-only-insecure-fallback-secret-key"
)

bcrypt = Bcrypt(app)


# =========================================================
# DISABLE BROWSER PAGE CACHING (Book Management staleness fix)
#
# The backend already updates the books table correctly on every
# issue/return (verified). The reported staleness is caused by
# the browser's back/forward cache (bfcache) and/or its normal
# HTTP cache: Flask sends no Cache-Control headers by default,
# so navigating back to Book Management (or in some browsers,
# a plain refresh) can restore an old, already-rendered copy of
# the page instead of asking the server for the current data.
#
# Marking every response "no-store" forces the browser to always
# fetch a fresh page from the server (and, in Chrome/Firefox/
# Safari, "no-store" specifically excludes the page from
# bfcache), so Book Management, Book Availability, Issue Books,
# etc. always show the live copies value. This only affects
# caching headers - it does not change any route, data, or
# template logic.
# =========================================================

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# =========================================================
# DATABASE REQUEST LIFECYCLE
#
# Every request starts and ends with a clean database state: any
# connection a code path forgot to close goes back to the pool, and the
# row snapshots remembered by load_*() (used by save_*() to write only
# what actually changed) never leak into the next request.
# =========================================================

@app.before_request
def _db_begin_request():
    db.end_request()


@app.teardown_request
def _db_end_request(exc):
    db.end_request()


@app.errorhandler(db.ConcurrentUpdateError)
def _handle_concurrent_update(error):
    flash(
        "Someone else changed the same information at the same moment, "
        "so your change was not saved (nothing was overwritten). "
        "Please review the page and try again.",
        "error"
    )
    return redirect(request.referrer or url_for("home"))


# =========================================================
# NOTIFICATION BADGE (available in every template)
# =========================================================

@app.context_processor
def inject_notification_count():

    if "email" not in session:
        return {"unread_notification_count": 0}

    try:
        notifications = load_notifications()
    except Exception:
        return {"unread_notification_count": 0}

    current_email = session.get("email", "").strip().lower()

    unread = [
        n for n in notifications
        if isinstance(n, dict)
        and n.get("user_email", "").strip().lower() == current_email
        and n.get("read") is False
    ]

    return {"unread_notification_count": len(unread)}


# =========================================================
# LIBRARY CHATBOT - READY-MADE QUESTIONS (available in
# every template that includes the chatbot widget)
# =========================================================

@app.context_processor
def inject_chatbot_questions():
    # The RAG chatbot only appears on the normal User Dashboard, so
    # only inject its ready-made questions for that role.
    if session.get("role") != "user":
        return {"chatbot_questions": []}
    return {"chatbot_questions": CHATBOT_QUESTIONS}



# =========================================================
# EMAIL CONFIGURATION
# =========================================================

# One server-side SENDER account/service for the whole application. The
# defaults are Gmail SMTP (smtp.gmail.com, port 587, TLS). Optional env
# vars let the administrator use a different SMTP provider instead
# (MAIL_SERVER, MAIL_PORT, MAIL_USE_TLS, MAIL_USE_SSL). The recipient of
# every OTP is the registered user's own email - never these settings.
def _env_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _env_port(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "").strip() or "smtp.gmail.com"
app.config["MAIL_PORT"] = _env_port("MAIL_PORT", 587)
app.config["MAIL_USE_SSL"] = _env_bool("MAIL_USE_SSL", False)
app.config["MAIL_USE_TLS"] = (
    False if app.config["MAIL_USE_SSL"] else _env_bool("MAIL_USE_TLS", True)
)

# Sender login (MAIL_USERNAME / MAIL_PASSWORD). For Gmail, MAIL_PASSWORD
# must be a Google App Password for MAIL_USERNAME. Optional MAIL_SENDER
# is the "From" address, for providers whose SMTP login is not an email
# address; it defaults to MAIL_USERNAME.
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "YOUR_GMAIL_USERNAME").strip()
# Google displays an App Password as "abcd efgh ijkl mnop"; the spaces
# are not part of the password.
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "YOUR_GMAIL_APP_PASSWORD").replace(" ", "").strip()
MAIL_SENDER_ADDRESS = (
    os.environ.get("MAIL_SENDER", "").strip() or app.config["MAIL_USERNAME"]
)

mail = Mail(app)

# =========================================================
# DATABASE (PostgreSQL - one central cloud database)
#
# ALL persistent data lives in a single hosted PostgreSQL database,
# reached through the DATABASE_URL environment variable. Every Admin,
# Librarian and User of the deployed application therefore reads and
# writes the same live data. There is no local database file and no JSON
# file any more.
#
# The load_*()/save_*() helpers below still return/accept the exact same
# "list of dicts" shape as before, so none of the business logic elsewhere
# in this file (issuing, returning, renewing, fines, waitlist, etc.) had to
# change. The connection pool and the query helpers live in db.py.
# =========================================================

BORROW_RECORD_COLUMNS = db.TABLES["borrow_records"]["cols"]
WAITLIST_COLUMNS = db.TABLES["waitlist"]["cols"]
NOTIFICATION_COLUMNS = db.TABLES["notifications"]["cols"]


def get_db_connection():
    return db.get_connection()


def init_database():
    """
    Creates any missing table (schema_postgres.sql is idempotent) and
    seeds the default settings. It never drops, truncates or overwrites
    existing data. An advisory lock makes several server workers starting
    at the same moment safe.
    """
    schema_path = os.path.join(BASE_DIR, "schema_postgres.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_text = re.sub(r"--[^\n]*", "", f.read())
    statements = [x.strip() for x in schema_text.split(";") if x.strip()]

    conn = get_db_connection()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(727274)")
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def get_system_setting(setting_name, default=None):
    """
    Reads one named value out of the system_settings table - the
    database-backed replacement for hardcoded business-rule
    constants. Returns `default` if the setting hasn't been seeded.
    """

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT setting_value FROM system_settings WHERE setting_name = ?",
        (setting_name,)
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        return default

    return row["setting_value"]


def get_borrow_days():
    """
    Standard borrowing period, in days, read from system_settings
    (BORROW_DURATION_DAYS). The backend always computes due dates
    from this value - the frontend never calculates it independently.
    """

    return int(get_system_setting("BORROW_DURATION_DAYS", 14))


def get_renewal_days():
    """
    Number of extra days a librarian-approved renewal adds to a
    book's due date, read from system_settings
    (RENEWAL_DURATION_DAYS).
    """

    return int(get_system_setting("RENEWAL_DURATION_DAYS", 7))


init_database()


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def load_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT email, name, password, role FROM users ORDER BY seq")
    rows = cur.fetchall()
    conn.close()
    db.remember_snapshot("users", rows)
    return [dict(row) for row in rows]


def save_users(users):
    """
    Writes only what changed compared with what this request loaded
    (see db.sync_table): added, edited and removed users - never a
    delete-and-reinsert of every account.
    """
    db.sync_table("users", [
        (u.get("email"), u.get("name"), u.get("password"), u.get("role"))
        for u in users
    ])


def find_user(email):
    users = load_users()

    email = email.strip().lower()

    for user in users:
        if user.get("email", "").strip().lower() == email:
            return user

    return None


def add_user(name, email, hashed_password, role):
    """
    Insert ONE new account. Used by the password-creating routes so they
    no longer rewrite the whole users table (save_users() deletes and
    re-inserts every user, which fails with a foreign-key error as soon
    as any renewal request exists).
    """
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO users (email, name, password, role) VALUES (?, ?, ?, ?)",
        (email, name, hashed_password, role)
    )
    conn.commit()
    conn.close()


def set_user_password(email, hashed_password):
    """Update ONE account's password hash (bcrypt) in place."""
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET password = ? WHERE lower(trim(email)) = ?",
        (hashed_password, email.strip().lower())
    )
    conn.commit()
    conn.close()


# =========================================================
# PASSWORD RULE
#
# NO length limit (no 8-character cap, no maximum at all). A password
# must contain at least one uppercase letter (A-Z), one lowercase
# letter (a-z), one digit (0-9) and one special character.
#
# bcrypt itself only reads the first 72 bytes of its input. So that
# longer passwords are fully honoured, any password over 72 bytes is
# first reduced to a SHA-256 hex digest (64 chars) and that is what is
# bcrypt-hashed. Passwords of 72 bytes or fewer are passed to bcrypt
# unchanged, so every existing user's stored hash keeps working.
# =========================================================

PASSWORD_POLICY_MESSAGE = (
    "Password must contain at least one uppercase letter, one "
    "lowercase letter, one number and one special character."
)


def validate_password_policy(password):
    """Return None if the password is acceptable, else the error text."""

    if not isinstance(password, str) or not password:
        return "Password cannot be empty."

    missing = []
    if not re.search(r"[A-Z]", password):
        missing.append("one uppercase letter")
    if not re.search(r"[a-z]", password):
        missing.append("one lowercase letter")
    if not re.search(r"[0-9]", password):
        missing.append("one number")
    if not re.search(r"[^A-Za-z0-9]", password):
        missing.append("one special character")

    if missing:
        if len(missing) == 1:
            text = missing[0]
        else:
            text = ", ".join(missing[:-1]) + " and " + missing[-1]
        return "Password must contain at least " + text + "."

    return None


def _bcrypt_input(password):
    raw = password.encode("utf-8")
    if len(raw) > 72:
        return hashlib.sha256(raw).hexdigest()
    return password


def hash_password(password):
    return bcrypt.generate_password_hash(
        _bcrypt_input(password)
    ).decode("utf-8")


def check_password(stored_hash, password):
    return bcrypt.check_password_hash(stored_hash, _bcrypt_input(password))


def _ensure_required_accounts():
    """
    Keep the required project admin/librarian credentials synchronized
    with the central PostgreSQL database used by the application.

    Passwords are stored only as bcrypt hashes. The target passwords are
    not kept in plaintext in source code.
    """
    required_accounts = [
        (
            "admin",
            "roshniarunnayak2005@gmail.com",
            "Roshni",
            "$2y$12$Cy7CA7bCWVY03xiPtM5C1uyeIG5gCpCARKVcKzMxWw5QcNAQeY.Ya",
        ),
        (
            "librarian",
            "princiamascarenhas20@gmail.com",
            "Princia",
            "$2y$12$nxdbIvsWW1v0C78qgjlAY.iM2hSGKLxnYnxLqaadcDp4IA6XEOed2",
        ),
    ]

    conn = get_db_connection()
    try:
        for role, target_email, name, password_hash in required_accounts:
            target_email = target_email.strip().lower()

            target = conn.execute(
                "SELECT email, role FROM users "
                "WHERE lower(trim(email)) = ?",
                (target_email,)
            ).fetchone()

            role_row = conn.execute(
                "SELECT email FROM users "
                "WHERE lower(trim(role)) = ? "
                "ORDER BY email LIMIT 1",
                (role,)
            ).fetchone()

            if target is not None and str(target["role"]).strip().lower() == role:
                conn.execute(
                    "UPDATE users SET name = ?, password = ?, role = ? "
                    "WHERE lower(trim(email)) = ?",
                    (name, password_hash, role, target_email)
                )
            elif target is None and role_row is not None:
                conn.execute(
                    "UPDATE users SET email = ?, name = ?, password = ?, role = ? "
                    "WHERE email = ?",
                    (target_email, name, password_hash, role, role_row["email"])
                )
            elif target is None and role_row is None:
                conn.execute(
                    "INSERT INTO users (email, name, password, role) "
                    "VALUES (?, ?, ?, ?)",
                    (target_email, name, password_hash, role)
                )
            else:
                # A different account already owns the requested email.
                # Do not silently overwrite unrelated user data.
                raise RuntimeError(
                    f"Cannot synchronize required {role} account because "
                    f"{target_email} already belongs to another role."
                )

        conn.commit()
    finally:
        conn.close()


_ensure_required_accounts()


# =========================================================
# FORGOT PASSWORD - EMAIL OTP HELPERS
#
# The OTP is generated with the secrets module, emailed through the
# existing Flask-Mail setup, and kept ONLY as a bcrypt hash in the
# password_reset_otps table. It is never placed in the session cookie
# (which the browser can read), the URL, a flash message, the HTML or
# the console. The session only carries the id of the reset request.
# =========================================================

OTP_VALIDITY_MINUTES = 5
OTP_RESET_WINDOW_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
_RESET_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def generate_otp():
    # Cryptographically secure, always exactly 6 digits (leading
    # zeros allowed).
    return f"{secrets.randbelow(1000000):06d}"


def _parse_reset_dt(text):
    try:
        return datetime.strptime(text, _RESET_DT_FORMAT)
    except (ValueError, TypeError):
        return None


def _clear_reset_session():
    for key in ("reset_id", "reset_email", "otp_attempts",
                "otp", "otp_expires_at"):
        session.pop(key, None)


def _get_reset_row(conn):
    reset_id = session.get("reset_id")
    reset_email = session.get("reset_email")

    if (not isinstance(reset_id, int) or isinstance(reset_id, bool)
            or reset_id <= 0 or not reset_email):
        return None

    return conn.execute(
        "SELECT * FROM password_reset_otps WHERE id = ? AND email = ?",
        (reset_id, reset_email)
    ).fetchone()


def _invalidate_reset_row(conn, reset_id):
    conn.execute(
        "UPDATE password_reset_otps SET used = 1, otp_hash = '' "
        "WHERE id = ?",
        (reset_id,)
    )
    conn.commit()


def _mail_missing_settings():
    """Names of the mail settings that are missing or still placeholders."""
    username = (app.config.get("MAIL_USERNAME") or "").strip()
    password = (app.config.get("MAIL_PASSWORD") or "").strip()
    missing = []
    if (not username or username.upper().startswith("YOUR_")
            or username.lower().startswith("your")):
        missing.append("MAIL_USERNAME")
    if (not password or password.upper().startswith("YOUR_")
            or password.lower() == "your-gmail-app-password"):
        missing.append("MAIL_PASSWORD")
    if "@" not in (MAIL_SENDER_ADDRESS or "") and "MAIL_USERNAME" not in missing:
        missing.append("MAIL_SENDER")
    return missing


def _mail_is_configured():
    return not _mail_missing_settings()


def _send_otp_email(recipient_email, otp):
    try:
        msg = Message(
            subject="Library Management System - Password Reset OTP",
            sender=MAIL_SENDER_ADDRESS,
            recipients=[recipient_email]
        )

        msg.body = (
            "Hello,\n\n"
            "A password reset was requested for your Library Management "
            "System account.\n\n"
            f"Your One-Time Password (OTP) is: {otp}\n\n"
            f"This OTP is valid for {OTP_VALIDITY_MINUTES} minutes and "
            "can only be used once.\n\n"
            "If you did not request a password reset, you can safely "
            "ignore this email - your password has not been changed.\n\n"
            "Thank you,\n"
            "Library Management System\n"
        )

        mail.send(msg)
        return True, ""

    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(
            "Sender authentication failed (SMTP %s) for %s.",
            e.smtp_code, app.config["MAIL_SERVER"]
        )
        return False, (
            "Email sender login failed: the server's MAIL_USERNAME / "
            "MAIL_PASSWORD were rejected by " + app.config["MAIL_SERVER"]
            + ". For Gmail, MAIL_PASSWORD must be a Google App Password "
            "for the sender account. This is a server configuration "
            "problem, not a problem with the user's email."
        )

    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as e:
        app.logger.error("Mail server refused an address (%s).",
                         type(e).__name__)
        return False, (
            "The mail server refused this email address. Please check "
            "that the registered email address is valid."
        )

    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            ConnectionError, TimeoutError, OSError) as e:
        app.logger.error("Could not reach the mail server (%s).",
                         type(e).__name__)
        return False, (
            "Could not connect to the mail server "
            + app.config["MAIL_SERVER"] + ":"
            + str(app.config["MAIL_PORT"])
            + ". Please check the server's internet connection."
        )

    except Exception as e:
        # Never log the OTP or the message body.
        app.logger.error(
            "Password reset email could not be sent (%s: %s).",
            type(e).__name__, e
        )
        return False, (
            "The OTP email could not be sent (" + type(e).__name__ + "). "
            "Please try again later."
        )


_missing_at_start = _mail_missing_settings()
if _missing_at_start:
    _env_candidates = [os.path.join(BASE_DIR, ".env"),
                       os.path.join(BASE_DIR, ".env.txt")]
    print(
        "WARNING: Forgot Password emails are disabled - missing/placeholder "
        + " and ".join(_missing_at_start) + "."
    )
    for _path in _env_candidates:
        print("  looked for: " + _path + " -> "
              + ("FOUND" if os.path.isfile(_path) else "not found"))
    print(
        "  Put MAIL_USERNAME and MAIL_PASSWORD in the .env file above "
        "(copy .env.example, replace the placeholder values), then restart."
    )
else:
    print("Email (Gmail SMTP) settings found - Forgot Password OTP emails enabled.")


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    return render_template("home.html")

# =========================================================
# DASHBOARD SELECTION
# =========================================================

@app.route("/dashboard-selection")
def dashboard_selection():
    """
    Backward-compatible route. Login no longer shows a role-selection
    screen; this route simply sends an authenticated user directly to
    the dashboard that matches the role stored in the database.
    """
    if "email" not in session:
        return redirect(url_for("login"))

    user_role = session.get("role", "user").lower()

    if user_role == "admin":
        return redirect(url_for("admin_dashboard"))
    if user_role == "librarian":
        return redirect(url_for("librarian_dashboard"))
    if user_role == "user":
        return redirect(url_for("dashboard"))

    session.clear()
    return redirect(url_for("login"))


# =========================================================
# SELECT ROLE
# =========================================================

@app.route("/select-role/<role>")
def select_role(role):
    """
    Legacy compatibility endpoint. Role switching is no longer part of
    the login flow. Always route the authenticated account to the role
    stored in its session/database record.
    """
    if "email" not in session:
        return redirect(url_for("login"))

    user_role = session.get("role", "user").lower()

    if user_role == "admin":
        return redirect(url_for("admin_dashboard"))
    if user_role == "librarian":
        return redirect(url_for("librarian_dashboard"))
    if user_role == "user":
        return redirect(url_for("dashboard"))

    session.clear()
    return redirect(url_for("login"))


# =========================================================
# COMMON BOOK PAGES
# =========================================================

@app.route("/search-books")
def search_books():

    if "email" not in session:
        return redirect(url_for("login"))

    return render_template("search_books.html")


@app.route("/view-books")
def view_books():

    if "email" not in session:
        return redirect(url_for("login"))

    books = load_books()

    return render_template(
        "view_books.html",
        books=books
    )

# =========================================================
# USER MANAGEMENT
# =========================================================

@app.route("/user-management")
def user_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        flash(
            "You are not authorized to access User Management.",
            "error"
        )
        return redirect(url_for("back_to_dashboard"))

    users = load_users()

    managed_users = []

    for user in users:

        # Only normal members belong in User Management.
        # Librarian accounts are managed from Librarian Management.
        if user.get("role") == "user":

            managed_users.append(user)

    return render_template(
        "user_management.html",
        users=managed_users
    )

# =========================================================
# OLD MEMBER MANAGEMENT URL
# =========================================================

@app.route("/member-management")
def member_management():

    return redirect(url_for("user_management"))


# =========================================================
# EDIT USER
# =========================================================

@app.route("/edit-user/<path:email>", methods=["GET", "POST"])
def edit_user(email):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        flash("You are not authorized to edit users.", "error")
        return redirect(url_for("back_to_dashboard"))

    users = load_users()

    selected_user = None

    for user in users:

        if user.get("email", "").strip().lower() == email.strip().lower():

            selected_user = user
            break

    if selected_user is None:

        flash("User not found.", "error")

        return redirect(url_for("user_management"))

    # Librarian cannot edit admin
    if (
        session.get("role") == "librarian"
        and selected_user.get("role") == "admin"
    ):

        flash("Librarians cannot edit admin accounts.", "error")

        return redirect(url_for("user_management"))

    # =====================================================
    # SAVE EDITED USER
    # =====================================================

    if request.method == "POST":

        new_name = request.form.get("name", "").strip()

        new_email = request.form.get("email", "").strip().lower()

        new_role = request.form.get(
            "role",
            selected_user.get("role", "user")
        ).strip().lower()

        # Name validation
        if not new_name:

            flash("Name cannot be empty.", "error")

            return redirect(
                url_for(
                    "edit_user",
                    email=email
                )
            )

        # Email validation
        if not new_email:

            flash("Email cannot be empty.", "error")

            return redirect(
                url_for(
                    "edit_user",
                    email=email
                )
            )

        # Only these roles can be assigned here
        if new_role not in ["user", "librarian"]:

            new_role = selected_user.get(
                "role",
                "user"
            )

        # Librarian cannot create admin
        if (
            session.get("role") == "librarian"
            and new_role == "admin"
        ):

            flash(
                "Librarians cannot assign admin role.",
                "error"
            )

            return redirect(
                url_for(
                    "edit_user",
                    email=email
                )
            )

        # =================================================
        # CHECK DUPLICATE EMAIL
        # =================================================

        old_email = selected_user.get(
            "email",
            ""
        ).strip().lower()

        for user in users:

            existing_email = user.get(
                "email",
                ""
            ).strip().lower()

            if (
                existing_email == new_email
                and existing_email != old_email
            ):

                flash(
                    "Another account already uses this email.",
                    "error"
                )

                return redirect(
                    url_for(
                        "edit_user",
                        email=email
                    )
                )

        # =================================================
        # UPDATE USER
        # =================================================

        selected_user["name"] = new_name

        selected_user["email"] = new_email

        selected_user["role"] = new_role

        save_users(users)

        # Update session if current user edited themselves
        if (
            session.get("email", "").strip().lower()
            == old_email
        ):

            session["email"] = new_email

            session["name"] = new_name

            session["role"] = new_role

        flash(
            "User updated successfully.",
            "success"
        )

        return redirect(
            url_for("user_management")
        )

    # =====================================================
    # OPEN EDIT PAGE
    # =====================================================

    return render_template(
        "edit_user.html",
        user=selected_user
    )


# =========================================================
# DELETE USER
# =========================================================

@app.route("/delete-user/<path:email>", methods=["GET", "POST"])
def delete_user(email):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:

        flash(
            "You are not authorized to delete users.",
            "error"
        )

        return redirect(
            url_for("back_to_dashboard")
        )

    users = load_users()

    target_user = None

    for user in users:

        if (
            user.get("email", "").strip().lower()
            == email.strip().lower()
        ):

            target_user = user
            break

    if target_user is None:

        flash(
            "User not found.",
            "error"
        )

        return redirect(
            url_for("user_management")
        )

    target_email = target_user.get(
        "email",
        ""
    ).strip().lower()

    logged_in_email = session.get(
        "email",
        ""
    ).strip().lower()

    # Cannot delete yourself
    if target_email == logged_in_email:

        flash(
            "You cannot delete your own account.",
            "error"
        )

        return redirect(
            url_for("user_management")
        )

    # Librarian cannot delete admin
    if (
        session.get("role") == "librarian"
        and target_user.get("role") == "admin"
    ):

        flash(
            "Librarians cannot delete admin accounts.",
            "error"
        )

        return redirect(
            url_for("user_management")
        )

    # =====================================================
    # BLOCK DELETION IF THE USER STILL HAS UNRETURNED BOOKS
    # (active borrow = borrow_records.returned is 0/NULL; a
    # returned book - returned = 1 - never blocks deletion)
    # =====================================================

    conn = get_db_connection()
    active_count = conn.execute(
        "SELECT COUNT(*) FROM borrow_records "
        "WHERE LOWER(TRIM(user_email)) = ? "
        "AND COALESCE(returned, 0) = 0",
        (target_email,)
    ).fetchone()[0]
    conn.close()

    if active_count > 0:

        flash(
            "Cannot delete user. This user still has borrowed book(s). "
            "Please ensure all books are returned before deleting the user.",
            "error"
        )

        return redirect(
            url_for("user_management")
        )

    # =====================================================
    # DELETE
    # Deletes ONLY this one user row (save_users() rewrites the whole
    # table, which fails once renewal_requests rows reference users).
    # The user's old renewal requests are removed in the same
    # transaction because they hold a foreign key to users.email.
    # =====================================================

    conn = get_db_connection()
    conn.execute(
        "DELETE FROM renewal_requests WHERE LOWER(TRIM(user_email)) = ?",
        (target_email,)
    )
    conn.execute(
        "DELETE FROM users WHERE LOWER(TRIM(email)) = ?",
        (target_email,)
    )
    conn.commit()
    conn.close()

    # Remove the deleted user's waitlist entries too, so no orphaned
    # records are left behind in the waitlist table.
    waitlist = [
        entry
        for entry in load_waitlist()
        if entry.get("user_email", "").strip().lower() != target_email
    ]
    save_waitlist(waitlist)

    flash(
        "User deleted successfully.",
        "success"
    )

    return redirect(
        url_for("user_management")
    )


# =========================================================
# BOOK MANAGEMENT
# =========================================================


# =========================================================
# BOOK MANAGEMENT
# =========================================================
@app.route("/book-management")
def book_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        return redirect(url_for("login"))

    books = load_books()

    return render_template(
        "book_management.html",
        books=books
    )

# =========================================================
# LIBRARIAN MANAGEMENT
# =========================================================

@app.route("/librarian-management")
def librarian_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    users = load_users()

    librarians = [
        user for user in users
        if user.get("role") == "librarian"
    ]

    return render_template(
        "librarian_management.html",
        librarians=librarians
    )


# =========================================================
# EDIT LIBRARIAN
# =========================================================

@app.route("/edit-librarian/<path:email>", methods=["GET", "POST"])
def edit_librarian(email):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    users = load_users()

    librarian = None

    for user in users:

        if (
            user.get("email", "").strip().lower() == email.strip().lower()
            and user.get("role") == "librarian"
        ):
            librarian = user
            break

    if librarian is None:
        flash("Librarian not found.", "error")
        return redirect(url_for("user_librarian_management"))

    if request.method == "POST":

        new_name = request.form.get("name", "").strip()
        new_email = request.form.get("email", "").strip().lower()

        if not new_name or not new_email:
            flash("Name and email are required.", "error")
            return redirect(url_for("edit_librarian", email=email))

        for user in users:

            if user is librarian:
                continue

            if user.get("email", "").strip().lower() == new_email:
                flash("Email already exists.", "error")
                return redirect(url_for("edit_librarian", email=email))

        librarian["name"] = new_name
        librarian["email"] = new_email

        save_users(users)

        flash("Librarian updated successfully.", "success")

        return redirect(url_for("user_librarian_management"))

    return render_template(
        "edit_librarian.html",
        librarian=librarian
    )


# =========================================================
# DELETE LIBRARIAN
# =========================================================

@app.route("/delete-librarian/<path:email>")
def delete_librarian(email):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    users = load_users()

    new_users = []

    deleted = False

    for user in users:

        if (
            user.get("email", "").strip().lower() == email.strip().lower()
            and user.get("role") == "librarian"
        ):
            deleted = True
            continue

        new_users.append(user)

    if deleted:
        save_users(new_users)
        flash("Librarian deleted successfully.", "success")
    else:
        flash("Librarian not found.", "error")

    return redirect(url_for("user_librarian_management"))


# =========================================================
# COMBINED USER & LIBRARIAN MANAGEMENT (ADMIN ONLY)
# =========================================================

@app.route("/user-librarian-management")
def user_librarian_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    users = load_users()

    # Only normal members belong under "Users"
    managed_users = [
        user for user in users
        if user.get("role") == "user"
    ]

    # Only librarian accounts belong under "Librarians"
    librarians = [
        user for user in users
        if user.get("role") == "librarian"
    ]

    return render_template(
        "user_librarian_management.html",
        users=managed_users,
        librarians=librarians
    )


# =========================================================
# ADMIN - CREATE NEW USER OR LIBRARIAN ACCOUNT
# =========================================================

@app.route("/create-account", methods=["GET", "POST"])
def create_account():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "user").strip().lower()

        # The admin may only manually create Users or Librarians
        # from this form (never Admin accounts).
        if role not in ["user", "librarian"]:
            role = "user"

        if not name or not email or not password:

            flash(
                "All fields are required.",
                "error"
            )

            return redirect(url_for("create_account", role=role))

        password_error = validate_password_policy(password)

        if password_error:

            flash(password_error, "error")

            return redirect(url_for("create_account", role=role))

        users = load_users()

        for user in users:

            if user.get("email", "").strip().lower() == email:

                flash(
                    "An account with this email already exists.",
                    "error"
                )

                return redirect(url_for("create_account", role=role))

        hashed_password = hash_password(password)

        add_user(name, email, hashed_password, role)

        flash(
            f"{role.capitalize()} account for '{name}' "
            f"created successfully.",
            "success"
        )

        return redirect(url_for("user_librarian_management"))

    # Optional ?role=user or ?role=librarian to pre-select /
    # lock the account type when arriving from a specific
    # "Add User" / "Add Librarian" button.
    locked_role = request.args.get("role", "").strip().lower()

    if locked_role not in ("user", "librarian"):
        locked_role = None

    return render_template(
        "create_account.html",
        locked_role=locked_role
    )


# =========================================================
# LIBRARIAN FUNCTIONS
# =========================================================

@app.route("/issue-books", methods=["GET", "POST"])
def issue_books():

    # =====================================================
    # LIBRARIAN LOGIN CHECK
    # =====================================================

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:
        flash("Only librarians can issue books.", "error")
        return redirect(url_for("back_to_dashboard"))

    # =====================================================
    # LOAD DATA
    # =====================================================

    users = load_users()
    books = load_books()
    borrow_records = load_borrow_records()

    # Only registered normal users can receive books
    members = [
        user for user in users
        if user.get("role") == "user"
    ]

    # Remove empty objects such as {}
    valid_books = [
        book for book in books
        if book.get("book_id")
    ]

    last_issued = None

    # =====================================================
    # ISSUE BOOK
    # =====================================================

    if request.method == "POST":

        # The librarian can identify the member using
        # either their name OR their email address.
        member_identifier = request.form.get(
            "user_identifier",
            request.form.get("user_email", "")
        ).strip()

        user_email = member_identifier.strip().lower()

        book_id_raw = request.form.get(
            "book_id",
            ""
        ).strip()

        # The Search Book field submits "ID - Book Name" once a
        # result is selected. Extract just the ID; a plain ID
        # (e.g. from an older client) still works unchanged.
        if " - " in book_id_raw:
            book_id = book_id_raw.split(" - ", 1)[0].strip()
        else:
            book_id = book_id_raw

        # -------------------------------------------------
        # BASIC VALIDATION
        # -------------------------------------------------

        if not member_identifier or not book_id:

            flash(
                "Please select both a user (by name or "
                "email) and a book.",
                "error"
            )

            return redirect(url_for("issue_books"))

        # -------------------------------------------------
        # FIND USER - BY EMAIL FIRST, THEN BY NAME
        # -------------------------------------------------

        selected_user = None

        for user in members:

            if (
                user.get("email", "").strip().lower()
                == user_email
            ):

                selected_user = user
                break

        if selected_user is None:

            for user in members:

                if (
                    user.get("name", "").strip().lower()
                    == member_identifier.strip().lower()
                ):

                    selected_user = user
                    break

        if selected_user is None:

            flash(
                "Selected user was not found. Please choose "
                "a user using their exact name or email.",
                "error"
            )

            return redirect(url_for("issue_books"))

        # Use the member's real email from here on
        user_email = selected_user.get(
            "email", ""
        ).strip().lower()

        # -------------------------------------------------
        # FIND BOOK
        # -------------------------------------------------

        selected_book = None

        for book in valid_books:

            if str(book.get("book_id")) == str(book_id):

                selected_book = book
                break

        if selected_book is None:

            flash(
                "Selected book was not found.",
                "error"
            )

            return redirect(url_for("issue_books"))

        # -------------------------------------------------
        # CHECK AVAILABLE COPIES
        # -------------------------------------------------

        try:
            copies = int(selected_book.get("copies", 0))
        except (TypeError, ValueError):
            copies = 0

        if copies <= 0:

            flash(
                "This book is currently unavailable.",
                "error"
            )

            return redirect(url_for("issue_books"))

        # -------------------------------------------------
        # PREVENT DUPLICATE ACTIVE BORROWING
        # -------------------------------------------------

        already_borrowed = False

        for record in borrow_records:

            if not isinstance(record, dict):
                continue

            if (
                record.get("user_email", "").strip().lower()
                == user_email
                and str(record.get("book_id"))
                == str(book_id)
                and record.get("returned") is False
            ):

                already_borrowed = True
                break

        if already_borrowed:

            flash(
                "This user already has this book issued.",
                "error"
            )

            return redirect(url_for("issue_books"))

        # =================================================
        # CREATE BORROW RECORD
        # =================================================

        from datetime import datetime, timedelta

        issue_datetime = datetime.now()

        due_datetime = (
            issue_datetime
            + timedelta(days=get_borrow_days())
        )

        borrow_record = {

            "user_email": user_email,

            "book_id": str(
                selected_book.get("book_id")
            ),

            "book_name": selected_book.get(
                "book_name",
                ""
            ),

            "author": selected_book.get(
                "author",
                ""
            ),

            "category": selected_book.get(
                "category",
                ""
            ),

            "borrowed_date": issue_datetime.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

            "due_date": due_datetime.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

            "returned": False,

            "returned_date": None
        }

        borrow_records.append(borrow_record)

        # -------------------------------------------------
        # DECREASE AVAILABLE COPIES
        # -------------------------------------------------

        selected_book["copies"] = copies - 1

        # -------------------------------------------------
        # SAVE BOTH FILES
        # -------------------------------------------------

        save_borrow_records(borrow_records)
        save_books(books)

        # -------------------------------------------------
        # BUILD CONFIRMATION DETAILS FOR THE LIBRARIAN
        # -------------------------------------------------

        last_issued = {
            "user_name": selected_user.get("name", ""),
            "user_email": selected_user.get("email", ""),
            "book_name": selected_book.get("book_name", ""),
            "book_id": str(selected_book.get("book_id", "")),
            "author": selected_book.get("author", ""),
            "category": selected_book.get("category", ""),
            "borrowed_date": borrow_record["borrowed_date"],
            "due_date": borrow_record["due_date"],
        }

        add_notification(
            selected_user.get("email", ""),
            f"'{last_issued['book_name']}' was issued to you. "
            f"Due back on {last_issued['due_date'][:10]}.",
            "borrow_success"
        )

        # Refresh members/books/records so the page below
        # reflects the change we just made.
        users = load_users()
        books = load_books()
        borrow_records = load_borrow_records()

        members = [
            user for user in users
            if user.get("role") == "user"
        ]

        valid_books = [
            book for book in books
            if book.get("book_id")
        ]

        flash(
            f"Book '{last_issued['book_name']}' issued "
            f"successfully to {last_issued['user_name']}.",
            "success"
        )

    # =====================================================
    # DISPLAY CURRENTLY ISSUED BOOKS
    # =====================================================

    active_records = []

    for record in borrow_records:

        if not isinstance(record, dict):
            continue

        if record.get("returned") is False:

            active_records.append(record)

    return render_template(
        "issue_books.html",
        members=members,
        books=valid_books,
        issued_books=active_records,
        last_issued=last_issued
    )


@app.route(
    "/librarian-return-books",
    methods=["GET", "POST"]
)
def librarian_return_books():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:

        flash(
            "Only librarians can return books.",
            "error"
        )

        return redirect(
            url_for("back_to_dashboard")
        )

    books = load_books()
    borrow_records = load_borrow_records()
    users = load_users()

    # =====================================================
    # RETURN BOOK
    # =====================================================

    if request.method == "POST":

        record_index = request.form.get(
            "record_index",
            ""
        ).strip()

        condition = request.form.get(
            "book_condition",
            "good"
        ).strip().lower()

        # -------------------------------------------------
        # VALIDATE RECORD INDEX
        # -------------------------------------------------

        if not record_index.isdigit():

            flash(
                "Invalid borrowing record.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        index = int(record_index)

        if (
            index < 0
            or index >= len(borrow_records)
        ):

            flash(
                "Borrowing record not found.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        record = borrow_records[index]

        if not isinstance(record, dict):

            flash(
                "Invalid borrowing record.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        # -------------------------------------------------
        # ALREADY RETURNED?
        # -------------------------------------------------

        if record.get("returned") is True:

            flash(
                "This book has already been returned.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        # -------------------------------------------------
        # VALIDATE CONDITION
        # -------------------------------------------------

        allowed_conditions = [
            "good",
            "damaged",
            "lost"
        ]

        if condition not in allowed_conditions:

            condition = "good"

        # -------------------------------------------------
        # RETURN DATE
        # -------------------------------------------------

        returned_datetime = datetime.now()

        returned_date = returned_datetime.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        # -------------------------------------------------
        # CALCULATE LATE DAYS USING RETURN DATE
        # -------------------------------------------------

        temp_record = record.copy()

        temp_record["returned"] = True

        temp_record["returned_date"] = returned_date

        # IMPORTANT: book_condition must be set on temp_record
        # BEFORE calculate_fine() runs, otherwise damage/lost
        # fines are silently computed as 0 because calculate_fine()
        # would fall back to its default "good" condition.
        temp_record["book_condition"] = condition

        fine_data = calculate_fine(
            temp_record
        )

        # -------------------------------------------------
        # SAVE FINE INFORMATION IN RECORD
        # -------------------------------------------------

        record["returned"] = True

        record["returned_date"] = returned_date

        record["book_condition"] = condition

        # The return has now been finalized by the librarian, so
        # this record is no longer a pending "return request".
        record["return_requested"] = False

        record["days_late"] = (
            fine_data["days_late"]
        )

        record["late_fine"] = (
            fine_data["late_fine"]
        )

        record["damage_fine"] = (
            fine_data["damage_fine"]
        )

        record["lost_fine"] = (
            fine_data["lost_fine"]
        )

        record["total_fine"] = (
            fine_data["total_fine"]
        )

        record["fine_status"] = (
            "Pending"
            if fine_data["total_fine"] > 0
            else "No Fine"
        )

        # -------------------------------------------------
        # FIND BOOK
        # -------------------------------------------------

        book_id = str(
            record.get("book_id", "")
        ).strip()

        selected_book = None

        for book in books:

            if (
                str(
                    book.get("book_id", "")
                ).strip()
                == book_id
            ):

                selected_book = book
                break

        # -------------------------------------------------
        # UPDATE BOOK COPIES
        # -------------------------------------------------

        if selected_book is not None:

            try:

                current_copies = int(
                    selected_book.get(
                        "copies",
                        0
                    )
                )

            except (TypeError, ValueError):

                current_copies = 0

            # Lost book does NOT come back into inventory.
            if condition == "lost":

                selected_book["copies"] = current_copies

            else:

                selected_book["copies"] = (
                    current_copies + 1
                )

        # -------------------------------------------------
        # SAVE DATA
        # -------------------------------------------------

        save_books(books)
        save_borrow_records(
            borrow_records
        )

        # -------------------------------------------------
        # FIND BORROWER NAME
        # -------------------------------------------------

        borrower_email = record.get(
            "user_email",
            ""
        ).strip().lower()

        borrower_name = borrower_email

        for user in users:

            if (
                user.get(
                    "email",
                    ""
                ).strip().lower()
                == borrower_email
            ):

                borrower_name = user.get(
                    "name",
                    borrower_email
                )

                break

        # -------------------------------------------------
        # NOTIFY THE BORROWER + PROMOTE NEXT WAITLIST USER
        # (skip promotion for lost books - no copy actually
        # came back into inventory)
        # -------------------------------------------------

        if borrower_email:

            add_notification(
                borrower_email,
                f"Your book '{record.get('book_name', 'Book')}' "
                f"was returned by the librarian.",
                "return_success"
            )

            # ---------------------------------------------------
            # FINE NOTIFICATION
            #
            # Only created when the existing fine calculation
            # (fine_data, computed above via calculate_fine())
            # actually produced a fine greater than zero. Uses the
            # exact same days_late / total_fine already recorded
            # on the borrow record - no separate fine logic here.
            # ---------------------------------------------------

            fine_notification = build_fine_notification(
                record,
                fine_data,
                condition
            )

            if fine_notification is not None:

                add_notification(
                    borrower_email,
                    fine_notification["message"],
                    "fine_issued",
                    extra=fine_notification["extra"]
                )

        promoted_record = None

        if condition != "lost":

            promoted_record = promote_next_waitlist_user(
                book_id,
                record.get("book_name", "the book")
            )

        # -------------------------------------------------
        # SUCCESS MESSAGE
        # -------------------------------------------------

        if fine_data["total_fine"] > 0:

            flash(
                f"{record.get('book_name', 'Book')} "
                f"returned by {borrower_name}. "
                f"Fine: ₹{fine_data['total_fine']}.",
                "success"
            )

        else:

            flash(
                f"{record.get('book_name', 'Book')} "
                f"returned successfully by "
                f"{borrower_name}. No fine.",
                "success"
            )

        if promoted_record is not None:

            flash(
                "Book returned and automatically issued to the "
                "next user in the waitlist.",
                "success"
            )

        return redirect(
            url_for("librarian_return_books")
        )

    # =====================================================
    # SHOW CURRENTLY BORROWED BOOKS
    # =====================================================

    active_records = []

    for index, record in enumerate(
        borrow_records
    ):

        if not isinstance(record, dict):
            continue

        if record.get("returned") is not False:
            continue

        record_copy = record.copy()

        record_copy["_index"] = index

        # -------------------------------------------------
        # FIND USER NAME
        # -------------------------------------------------

        user_email = record.get(
            "user_email",
            ""
        ).strip().lower()

        user_name = user_email

        for user in users:

            if (
                user.get(
                    "email",
                    ""
                ).strip().lower()
                == user_email
            ):

                user_name = user.get(
                    "name",
                    user_email
                )

                break

        record_copy["user_name"] = user_name

        # -------------------------------------------------
        # RETURN REQUEST FLAG (set by the user on the User
        # Dashboard's "Request Return" action) so the librarian
        # can see which books are waiting on a return request.
        # -------------------------------------------------

        record_copy["return_requested"] = (
            record.get("return_requested") is True
        )

        # -------------------------------------------------
        # CURRENT FINE IF ALREADY LATE
        # -------------------------------------------------

        current_fine = calculate_fine(
            record
        )

        record_copy["current_days_late"] = (
            current_fine["days_late"]
        )

        record_copy["current_late_fine"] = (
            current_fine["late_fine"]
        )

        active_records.append(
            record_copy
        )

    return render_template(
        "librarian_return_books.html",
        borrowed_books=active_records
    )
    # =====================================================
    # RETURN BOOK
    # =====================================================

    if request.method == "POST":

        record_index = request.form.get(
            "record_index",
            ""
        ).strip()

        if not record_index.isdigit():

            flash(
                "Invalid borrowing record.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        index = int(record_index)

        if index < 0 or index >= len(borrow_records):

            flash(
                "Borrowing record not found.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        record = borrow_records[index]

        if not isinstance(record, dict):

            flash(
                "Invalid borrowing record.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        # -------------------------------------------------
        # CHECK WHETHER ALREADY RETURNED
        # -------------------------------------------------

        if record.get("returned") is True:

            flash(
                "This book has already been returned.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        book_id = str(
            record.get("book_id", "")
        ).strip()

        book_name = record.get(
            "book_name",
            "Book"
        )

        borrower_email = record.get(
            "user_email",
            ""
        ).strip().lower()

        # =================================================
        # MARK ORIGINAL BORROW RECORD AS RETURNED
        # =================================================

        record["returned"] = True

        record["returned_date"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        # =================================================
        # FIND BOOK
        # =================================================

        selected_book = None

        for book in books:

            if (
                str(book.get("book_id", "")).strip()
                == book_id
            ):

                selected_book = book
                break

        if selected_book is None:

            save_borrow_records(borrow_records)

            flash(
                f"Borrow record returned, but book "
                f"'{book_id}' was not found in the book records.",
                "error"
            )

            return redirect(
                url_for("librarian_return_books")
            )

        # =================================================
        # INCREASE AVAILABLE COPY
        # =================================================

        try:
            current_copies = int(
                selected_book.get("copies", 0)
            )
        except (TypeError, ValueError):
            current_copies = 0

        selected_book["copies"] = current_copies + 1

        # =================================================
        # FIND FIRST WAITLISTED USER
        # =================================================

        waitlist_position = None

        for position, waiting_user in enumerate(waitlist):

            if not isinstance(waiting_user, dict):
                continue

            if (
                str(
                    waiting_user.get("book_id", "")
                ).strip()
                == book_id
            ):

                waitlist_position = position
                break

        # =================================================
        # AUTOMATIC WAITLIST PROCESSING
        # =================================================

        automatic_issue = False
        waiting_user_name = None
        waiting_user_email = None

        if waitlist_position is not None:

            waiting_user = waitlist.pop(
                waitlist_position
            )

            waiting_user_email = (
                waiting_user.get(
                    "user_email",
                    ""
                ).strip().lower()
            )

            waiting_user_name = waiting_user.get(
                "user_name",
                waiting_user_email
            )

            # -------------------------------------------------
            # VERIFY WAITLIST USER STILL EXISTS
            # -------------------------------------------------

            actual_user = None

            for user in users:

                if (
                    user.get("email", "").strip().lower()
                    == waiting_user_email
                ):

                    actual_user = user
                    break

            if actual_user is not None:

                waiting_user_name = actual_user.get(
                    "name",
                    waiting_user_name
                )

                # ---------------------------------------------
                # CHECK USER DOES NOT ALREADY HAVE THIS BOOK
                # ---------------------------------------------

                already_has_book = False

                for existing_record in borrow_records:

                    if not isinstance(
                        existing_record,
                        dict
                    ):
                        continue

                    if (
                        existing_record.get(
                            "user_email",
                            ""
                        ).strip().lower()
                        == waiting_user_email
                        and str(
                            existing_record.get(
                                "book_id",
                                ""
                            )
                        ).strip()
                        == book_id
                        and existing_record.get(
                            "returned"
                        ) is False
                    ):

                        already_has_book = True
                        break

                # ---------------------------------------------
                # AUTOMATICALLY ISSUE RETURNED COPY
                # ---------------------------------------------

                if not already_has_book:

                    from datetime import timedelta

                    issue_datetime = datetime.now()

                    due_datetime = (
                        issue_datetime
                        + timedelta(days=get_borrow_days())
                    )

                    new_borrow_record = {

                        "user_email":
                            waiting_user_email,

                        "book_id":
                            book_id,

                        "book_name":
                            selected_book.get(
                                "book_name",
                                book_name
                            ),

                        "author":
                            selected_book.get(
                                "author",
                                ""
                            ),

                        "category":
                            selected_book.get(
                                "category",
                                ""
                            ),

                        "borrowed_date":
                            issue_datetime.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),

                        "due_date":
                            due_datetime.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),

                        "returned":
                            False,

                        "returned_date":
                            None
                    }

                    borrow_records.append(
                        new_borrow_record
                    )

                    # The returned copy is immediately
                    # assigned to the waiting user.
                    selected_book["copies"] -= 1

                    automatic_issue = True

        # =================================================
        # SAVE EVERYTHING
        # =================================================

        save_books(books)
        save_borrow_records(borrow_records)
        save_waitlist(waitlist)

        # =================================================
        # SUCCESS MESSAGE
        # =================================================

        if automatic_issue:

            flash(
                f"'{book_name}' returned successfully. "
                f"The book was automatically issued to "
                f"{waiting_user_name} from the waitlist.",
                "success"
            )

        else:

            flash(
                f"'{book_name}' returned successfully.",
                "success"
            )

        return redirect(
            url_for("librarian_return_books")
        )

    # =====================================================
    # DISPLAY CURRENTLY ISSUED BOOKS
    # =====================================================

    active_records = []

    for index, record in enumerate(
        borrow_records
    ):

        if not isinstance(record, dict):
            continue

        if record.get("returned") is not False:
            continue

        record_copy = record.copy()

        record_copy["_index"] = index

        # -------------------------------------------------
        # FIND ACTUAL USER NAME
        # -------------------------------------------------

        user_email = record.get(
            "user_email",
            ""
        ).strip().lower()

        user_name = user_email

        for user in users:

            if (
                user.get("email", "").strip().lower()
                == user_email
            ):

                user_name = user.get(
                    "name",
                    user_email
                )

                break

        record_copy["user_name"] = user_name

        active_records.append(
            record_copy
        )

    return render_template(
        "librarian_return_books.html",
        borrowed_books=active_records
    )
    # =====================================================
    # RETURN BOOK
    # =====================================================

    if request.method == "POST":

        record_index = request.form.get("record_index", "").strip()

        if not record_index.isdigit():
            flash("Invalid borrowing record.", "error")
            return redirect(url_for("librarian_return_books"))

        index = int(record_index)

        if index < 0 or index >= len(borrow_records):
            flash("Borrowing record not found.", "error")
            return redirect(url_for("librarian_return_books"))

        record = borrow_records[index]

        if not isinstance(record, dict):
            flash("Invalid borrowing record.", "error")
            return redirect(url_for("librarian_return_books"))

        # Already returned
        if record.get("returned") is True:
            flash("This book has already been returned.", "error")
            return redirect(url_for("librarian_return_books"))

        book_id = str(record.get("book_id", "")).strip()

        # =================================================
        # MARK BORROW RECORD AS RETURNED
        # =================================================

        record["returned"] = True
        record["returned_date"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        # =================================================
        # INCREASE AVAILABLE COPIES
        # =================================================

        book_found = False

        for book in books:

            if str(book.get("book_id", "")).strip() == book_id:

                try:
                    current_copies = int(book.get("copies", 0))
                except (TypeError, ValueError):
                    current_copies = 0

                book["copies"] = current_copies + 1
                book_found = True
                break

        if not book_found:
            flash(
                "Book record was not found, but the borrowing record was updated.",
                "error"
            )

        # =================================================
        # SAVE DATA
        # =================================================

        save_books(books)
        save_borrow_records(borrow_records)

        borrower_name = record.get("user_email", "Unknown User")

        for user in users:
            if (
                user.get("email", "").strip().lower()
                == record.get("user_email", "").strip().lower()
            ):
                borrower_name = user.get("name", borrower_name)
                break

        flash(
            f"'{record.get('book_name', 'Book')}' returned successfully "
            f"by {borrower_name}.",
            "success"
        )

        return redirect(url_for("librarian_return_books"))

    # =====================================================
    # DISPLAY ACTIVE BORROWINGS
    # =====================================================

    active_records = []

    for index, record in enumerate(borrow_records):

        if not isinstance(record, dict):
            continue

        if record.get("returned") is False:

            record_copy = record.copy()
            record_copy["_index"] = index

            # Find actual user name
            user_name = record.get("user_email", "Unknown User")

            for user in users:

                if (
                    user.get("email", "").strip().lower()
                    == record.get("user_email", "").strip().lower()
                ):
                    user_name = user.get("name", user_name)
                    break

            record_copy["user_name"] = user_name

            active_records.append(record_copy)

    return render_template(
        "librarian_return_books.html",
        borrowed_books=active_records
    )

@app.route("/librarian-renew-books", methods=["GET", "POST"])
def librarian_renew_books():

    # =====================================================
    # LIBRARIAN/ADMIN LOGIN CHECK
    # =====================================================

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:
        flash("Only librarians can renew books.", "error")
        return redirect(url_for("back_to_dashboard"))

    # =====================================================
    # APPROVE / REJECT A PENDING RENEWAL REQUEST
    # (shared logic - see approve_renewal_request() /
    # reject_renewal_request())
    # =====================================================

    if request.method == "POST":

        renewal_request_id = request.form.get("renewal_request_id", "").strip()
        decision = request.form.get("decision", "").strip().lower()

        if not renewal_request_id.isdigit() or decision not in ("approve", "reject"):
            flash("Invalid renewal request.", "error")
            return redirect(url_for("librarian_renew_books"))

        try:
            if decision == "approve":
                approve_renewal_request(
                    renewal_request_id=int(renewal_request_id),
                    librarian_email=session.get("email")
                )
                flash("Renewal request approved.", "success")
            else:
                reject_renewal_request(
                    renewal_request_id=int(renewal_request_id),
                    librarian_email=session.get("email")
                )
                flash("Renewal request rejected.", "success")
        except RenewalError as err:
            flash(err.message, "error")

        return redirect(url_for("librarian_renew_books"))

    # =====================================================
    # SHOW PENDING RENEWAL REQUESTS
    # =====================================================

    return render_template(
        "librarian_renew_books.html",
        renewal_requests=load_staff_renewal_requests(status="PENDING"),
        renew_action_endpoint="librarian_renew_books"
    )

@app.route("/waitlist-management", methods=["GET", "POST"])
def waitlist_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:
        flash("Only librarians can manage the waitlist.", "error")
        return redirect(url_for("back_to_dashboard"))

    waitlist = load_waitlist()
    users = load_users()
    books = load_books()

    # =====================================================
    # REMOVE FROM WAITLIST
    # =====================================================

    if request.method == "POST":

        waitlist_index = request.form.get(
            "waitlist_index",
            ""
        ).strip()

        if not waitlist_index.isdigit():

            flash(
                "Invalid waitlist record.",
                "error"
            )

            return redirect(
                url_for("waitlist_management")
            )

        index = int(waitlist_index)

        if index < 0 or index >= len(waitlist):

            flash(
                "Waitlist record not found.",
                "error"
            )

            return redirect(
                url_for("waitlist_management")
            )

        removed = waitlist.pop(index)

        save_waitlist(waitlist)

        flash(
            f"{removed.get('user_name', 'User')} "
            f"was removed from the waitlist.",
            "success"
        )

        return redirect(
            url_for("waitlist_management")
        )

    # =====================================================
    # BUILD REAL WAITLIST DATA
    # =====================================================

    display_waitlist = []

    for index, entry in enumerate(waitlist):

        if not isinstance(entry, dict):
            continue

        item = entry.copy()

        item["_index"] = index

        # Find actual user
        user_email = entry.get(
            "user_email",
            ""
        ).strip().lower()

        user_name = entry.get(
            "user_name",
            user_email
        )

        for user in users:

            if (
                user.get("email", "").strip().lower()
                == user_email
            ):

                user_name = user.get(
                    "name",
                    user_name
                )

                break

        item["user_name"] = user_name

        # Find actual book
        book_id = str(
            entry.get("book_id", "")
        ).strip()

        book_name = entry.get(
            "book_name",
            book_id
        )

        for book in books:

            if (
                str(book.get("book_id", "")).strip()
                == book_id
            ):

                book_name = book.get(
                    "book_name",
                    book_name
                )

                break

        item["book_name"] = book_name

        display_waitlist.append(item)

    return render_template(
        "waitlist_management.html",
        waitlist=display_waitlist
    )
@app.route(
    "/fine-management",
    methods=["GET", "POST"]
)
def fine_management():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:

        flash(
            "Only librarians can manage fines.",
            "error"
        )

        return redirect(
            url_for("back_to_dashboard")
        )

    borrow_records = load_borrow_records()
    users = load_users()

    # =====================================================
    # MARK FINE AS PAID
    # =====================================================

    if request.method == "POST":

        record_index = request.form.get(
            "record_index",
            ""
        ).strip()

        if not record_index.isdigit():

            flash(
                "Invalid fine record.",
                "error"
            )

            return redirect(
                url_for("fine_management")
            )

        index = int(record_index)

        if (
            index < 0
            or index >= len(borrow_records)
        ):

            flash(
                "Fine record not found.",
                "error"
            )

            return redirect(
                url_for("fine_management")
            )

        record = borrow_records[index]

        if not isinstance(record, dict):

            flash(
                "Invalid fine record.",
                "error"
            )

            return redirect(
                url_for("fine_management")
            )

        if record.get("returned") is not True:

            flash(
                "A fine cannot be marked paid before the book is returned.",
                "error"
            )

            return redirect(
                url_for("fine_management")
            )

        # Already paid - clicking again (double click, back button,
        # resubmission, etc.) must not re-process or error out.
        if record.get("fine_status") == "Paid":

            flash(
                "This fine has already been marked as paid.",
                "success"
            )

            return redirect(
                url_for("fine_management")
            )

        # Use the SAME dynamic calculation Fine Management displays,
        # rather than trusting a stored total_fine value. Some records
        # (e.g. older ones, or books returned before fine fields were
        # being saved on every return path) may not have total_fine
        # stored at all, which previously made this incorrectly think
        # there was no fine to pay.
        fine_data = calculate_fine(record)

        if fine_data["total_fine"] <= 0:

            flash(
                "This record has no fine.",
                "error"
            )

            return redirect(
                url_for("fine_management")
            )

        # Persist the authoritative fine breakdown alongside the
        # paid status, so the record is self-consistent afterward.
        record["days_late"] = fine_data["days_late"]
        record["late_fine"] = fine_data["late_fine"]
        record["damage_fine"] = fine_data["damage_fine"]
        record["lost_fine"] = fine_data["lost_fine"]
        record["total_fine"] = fine_data["total_fine"]

        record["fine_status"] = "Paid"

        record["fine_paid_date"] = (
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        save_borrow_records(
            borrow_records
        )

        flash(
            "Fine marked as paid successfully.",
            "success"
        )

        return redirect(
            url_for("fine_management")
        )

    # =====================================================
    # BUILD FINE LIST
    # =====================================================

    fines = []

    for index, record in enumerate(
        borrow_records
    ):

        if not isinstance(record, dict):
            continue

        # For an already-paid fine, use the stored fine
        # breakdown that was frozen at the moment it was paid,
        # instead of recalculating it. Everything else (still
        # outstanding fines, including active overdue books)
        # is calculated dynamically, since an active overdue
        # book gets ₹50 added for every extra late day.

        if record.get("fine_status") == "Paid":

            fine_data = {
                "days_late": record.get("days_late", 0),
                "late_fine": record.get("late_fine", 0),
                "damage_fine": record.get("damage_fine", 0),
                "lost_fine": record.get("lost_fine", 0),
                "total_fine": record.get("total_fine", 0),
            }

        else:

            fine_data = calculate_fine(
                record
            )

        total_fine = fine_data["total_fine"]

        # -------------------------------------------------
        # ONLY SHOW RECORDS WITH A FINE
        # -------------------------------------------------

        if total_fine <= 0:
            continue

        user_email = record.get(
            "user_email",
            ""
        ).strip().lower()

        member_name = user_email

        for user in users:

            if (
                user.get(
                    "email",
                    ""
                ).strip().lower()
                == user_email
            ):

                member_name = user.get(
                    "name",
                    user_email
                )

                break

        # -------------------------------------------------
        # CONDITION
        # -------------------------------------------------

        condition = record.get(
            "book_condition",
            "Good"
        )

        condition = str(
            condition
        ).capitalize()

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        if record.get("fine_status"):

            status = record.get(
                "fine_status"
            )

        else:

            if record.get("returned") is True:

                status = "Pending"

            else:

                status = "Pending"

        fine_item = {

            "_index":
                index,

            "member_name":
                member_name,

            "user_email":
                user_email,

            "book_name":
                record.get(
                    "book_name",
                    "Unknown Book"
                ),

            "book_id":
                record.get(
                    "book_id",
                    ""
                ),

            "due_date":
                record.get(
                    "due_date",
                    "Not available"
                ),

            "returned_date":
                record.get(
                    "returned_date",
                    "Not returned"
                ),

            "days_late":
                fine_data["days_late"],

            "late_fine":
                fine_data["late_fine"],

            "damage_fine":
                fine_data["damage_fine"],

            "lost_fine":
                fine_data["lost_fine"],

            "total_fine":
                total_fine,

            "condition":
                condition,

            "status":
                status,

            "returned":
                record.get(
                    "returned",
                    False
                )
        }

        fines.append(
            fine_item
        )

    return render_template(
        "fine_management.html",
        fines=fines
    )



@app.route("/book-availability")
def book_availability():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:
        return redirect(url_for("login"))

    # Load real books data so this page always reflects the
    # actual data source used by Book Management, instead of
    # showing static placeholder text.
    books = load_books()

    valid_books = [
        book for book in books
        if book.get("book_id")
    ]

    return render_template(
        "book_availability.html",
        books=valid_books
    )


@app.route("/borrow-records")
def borrow_records():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["librarian", "admin"]:
        return redirect(url_for("login"))

    # Load actual borrow records
    borrow_records = load_borrow_records()

    # Load actual users
    users = load_users()

    # Load actual books
    books = load_books()

    display_records = []

    for index, record in enumerate(borrow_records):

        # Ignore empty/invalid records
        if not isinstance(record, dict):
            continue

        if not record.get("user_email"):
            continue

        item = record.copy()

        item["_index"] = index

        # ============================================
        # FIND ACTUAL USER NAME
        # ============================================

        user_email = str(
            record.get("user_email", "")
        ).strip().lower()

        user_name = user_email

        for user in users:

            if (
                str(
                    user.get("email", "")
                ).strip().lower()
                == user_email
            ):

                user_name = user.get(
                    "name",
                    user_email
                )

                break

        item["user_name"] = user_name

        # ============================================
        # FIND ACTUAL BOOK NAME
        # ============================================

        book_id = str(
            record.get("book_id", "")
        ).strip()

        book_name = record.get(
            "book_name",
            book_id
        )

        for book in books:

            if (
                str(
                    book.get("book_id", "")
                ).strip()
                == book_id
            ):

                book_name = book.get(
                    "book_name",
                    book_name
                )

                break

        item["book_name"] = book_name

        # ============================================
        # STATUS
        # ============================================

        if record.get("returned") is True:

            item["status"] = "Returned"

        else:

            item["status"] = "Borrowed"

        # ============================================
        # DUE DATE FOR OLD RECORDS
        # ============================================

        if not item.get("due_date"):

            try:

                borrowed_date = datetime.strptime(
                    item.get("borrowed_date"),
                    "%Y-%m-%d %H:%M:%S"
                )

                due_date = (
                    borrowed_date
                    + timedelta(days=get_borrow_days())
                )

                item["due_date"] = (
                    due_date.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

            except (ValueError, TypeError):

                item["due_date"] = "Not available"

        display_records.append(item)

    # =====================================================
    # SEARCH BY MEMBER NAME
    # =====================================================

    search_query = request.args.get("q", "").strip()

    if search_query:

        query_lower = search_query.lower()

        display_records = [
            record for record in display_records
            if query_lower in str(
                record.get("user_name", "")
            ).lower()
        ]

    return render_template(
        "borrow_records.html",
        borrow_records=display_records,
        search_query=search_query
    )

# =========================================================
# ADMIN RENEW BOOKS
# =========================================================

@app.route("/admin-renew-books", methods=["GET", "POST"])
def admin_renew_books():

    # =====================================================
    # ADMIN LOGIN CHECK
    # =====================================================

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    # =====================================================
    # APPROVE / REJECT A PENDING RENEWAL REQUEST - reuses the exact
    # same shared logic as the librarian route, so admin never has
    # different renewal rules.
    # =====================================================

    if request.method == "POST":

        renewal_request_id = request.form.get("renewal_request_id", "").strip()
        decision = request.form.get("decision", "").strip().lower()

        if not renewal_request_id.isdigit() or decision not in ("approve", "reject"):
            flash("Invalid renewal request.", "error")
            return redirect(url_for("admin_renew_books"))

        try:
            if decision == "approve":
                approve_renewal_request(
                    renewal_request_id=int(renewal_request_id),
                    librarian_email=session.get("email")
                )
                flash("Renewal request approved.", "success")
            else:
                reject_renewal_request(
                    renewal_request_id=int(renewal_request_id),
                    librarian_email=session.get("email")
                )
                flash("Renewal request rejected.", "success")
        except RenewalError as err:
            flash(err.message, "error")

        return redirect(url_for("admin_renew_books"))

    # =====================================================
    # SHOW PENDING RENEWAL REQUESTS
    # =====================================================

    return render_template(
        "librarian_renew_books.html",
        renewal_requests=load_staff_renewal_requests(status="PENDING"),
        renew_action_endpoint="admin_renew_books"
    )
@app.route('/add-book', methods=['GET', 'POST'])
def add_book():

    if request.method == 'GET':
        return render_template('add_book.html')

    # POST = Save the book
    book_id = request.form.get('book_id')
    book_name = request.form.get('book_name')
    author = request.form.get('author')
    publisher = request.form.get('publisher')
    isbn = request.form.get('isbn')
    category = request.form.get('category')
    copies = request.form.get('copies')

    try:
        copies = int(copies)
    except ValueError:
        flash("Copies must be a number.", "error")
        return redirect(url_for("book_management"))

    # Load existing books
    books = load_books()

    # Check duplicate Book ID
    for book in books:
        if book.get("book_id") == book_id:
            flash("Book ID already exists.", "error")
            return redirect(url_for("book_management"))

    # Create new book
    new_book = {
        "book_id": book_id,
        "book_name": book_name,
        "author": author,
        "publisher": publisher,
        "isbn": isbn,
        "category": category,
        "copies": copies
    }

    # Add book
    books.append(new_book)

    # Save book
    save_books(books)

    flash("Book added successfully!", "success")

    return redirect(url_for("book_management"))


@app.route("/edit-book/<book_id>", methods=["GET", "POST"])
def edit_book(book_id):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        return redirect(url_for("login"))

    # Load books
    books = load_books()

    # Find the selected book
    book = None

    for item in books:
        if item.get("book_id") == book_id:
            book = item
            break

    # Book not found
    if book is None:
        flash("Book not found.", "error")
        return redirect(url_for("book_management"))

    # Save edited book
    if request.method == "POST":

        book["book_name"] = request.form.get(
            "book_name", ""
        ).strip()

        book["author"] = request.form.get(
            "author", ""
        ).strip()

        book["publisher"] = request.form.get(
            "publisher", ""
        ).strip()

        book["isbn"] = request.form.get(
            "isbn", ""
        ).strip()

        book["category"] = request.form.get(
            "category", ""
        ).strip()

        copies = request.form.get(
            "copies", "0"
        ).strip()

        try:
            book["copies"] = int(copies)
        except ValueError:
            flash("Copies must be a number.", "error")
            return redirect(
                url_for("edit_book", book_id=book_id)
            )

        # Save changes to the database
        save_books(books)

        flash("Book updated successfully!", "success")

        return redirect(url_for("book_management"))

    # Show edit page
    return render_template(
        "edit_book.html",
        book=book
    )



@app.route("/delete-book/<book_id>")
def delete_book(book_id):

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        return redirect(url_for("login"))

    # Load books
    books = load_books()

    # Remove selected book
    updated_books = []

    book_found = False

    for book in books:

        if book.get("book_id") == book_id:
            book_found = True
        else:
            updated_books.append(book)

    if not book_found:
        flash("Book not found.", "error")
        return redirect(url_for("book_management"))

    # Save updated books
    save_books(updated_books)

    flash("Book deleted successfully!", "success")

    return redirect(url_for("book_management"))

# =========================================================
# REPORTS
# =========================================================

@app.route("/reports")
def reports():

    # Check login
    if "email" not in session:
        return redirect(url_for("login"))

    # Admin and Librarian only
    if session.get("role") not in ["admin", "librarian"]:
        return redirect(url_for("login"))

    # =====================================================
    # TOTAL BOOKS
    # =====================================================

    books = load_books()

    # Count every book record as one book
    total_books = len(books)


    # =====================================================
    # TOTAL USERS
    # =====================================================

    users = load_users()

    # Count only normal users
    total_users = sum(
        1 for user in users
        if user.get("role", "").lower() == "user"
    )


    # =====================================================
    # BOOKS CURRENTLY ISSUED
    # =====================================================

    borrow_records = load_borrow_records()

    # Count records where book has NOT been returned
    total_issued = sum(
        1 for record in borrow_records
        if record.get("returned") is False
    )


    # =====================================================
    # TOTAL OUTSTANDING FINE
    # =====================================================

    total_fine = 0

    # Use the SAME fine calculation as Fine Management, so the
    # Reports total always matches what Fine Management shows.
    # Only unpaid/outstanding fines are counted as "Total Fine".
    for record in borrow_records:

        if not isinstance(record, dict):
            continue

        if record.get("fine_status") == "Paid":
            continue

        fine_data = calculate_fine(record)

        total_fine += fine_data["total_fine"]


    # =====================================================
    # SEND REAL DATA TO REPORTS PAGE
    # =====================================================

    return render_template(
        "reports.html",
        total_books=total_books,
        total_users=total_users,
        total_issued=total_issued,
        total_fine=total_fine
    )

# =========================================================
# CATEGORIES
# =========================================================

@app.route("/categories")
def categories():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["admin", "librarian"]:
        return redirect(url_for("login"))

    return render_template("categories.html")


# =========================================================
# USER - BORROW BOOKS
# =========================================================
@app.route("/borrow_books", methods=["GET", "POST"])
def borrow_books():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))

    books = load_books()
    borrow_records = load_borrow_records()
    waitlist = load_waitlist()

    current_email = session.get("email", "").strip().lower()
    current_name = session.get("name", current_email)

    if request.method == "POST":

        book_id = request.form.get("book_id", "").strip()

        if not book_id:

            flash("Please select a book to borrow.", "error")

            return redirect(url_for("borrow_books"))

        # -------------------------------------------------
        # FIND BOOK
        # -------------------------------------------------

        selected_book = None

        for book in books:

            if str(book.get("book_id", "")).strip() == book_id:

                selected_book = book
                break

        if selected_book is None:

            flash(
                "Book ID not found. Please select a valid book.",
                "error"
            )

            return redirect(url_for("borrow_books"))

        # -------------------------------------------------
        # CHECK WHETHER USER CURRENTLY HAS THIS BOOK BORROWED
        # (active/unreturned only).
        #
        # A single user may not hold two copies of the same book
        # at the same time - matched on their own email + the
        # book's unique book_id (not just title). Once they return
        # it (record.returned becomes True), they are free to
        # borrow that same book again.
        # -------------------------------------------------

        for record in borrow_records:

            if not isinstance(record, dict):
                continue

            if (
                str(record.get("user_email", "")).strip().lower()
                == current_email
                and str(record.get("book_id", "")).strip() == book_id
                and record.get("returned") is False
            ):

                flash(
                    "\u26a0\ufe0f You can only borrow one copy of the "
                    "same book at a time. You already have this "
                    "book borrowed.",
                    "error"
                )

                return redirect(url_for("borrow_books"))

        # -------------------------------------------------
        # CHECK WHETHER USER IS ALREADY ON THE WAITLIST
        # -------------------------------------------------

        for entry in waitlist:

            if not isinstance(entry, dict):
                continue

            if (
                str(entry.get("user_email", "")).strip().lower()
                == current_email
                and str(entry.get("book_id", "")).strip() == book_id
            ):

                flash(
                    "You are already on the waitlist for this book.",
                    "warning"
                )

                return redirect(url_for("borrow_books"))

        try:
            copies = int(selected_book.get("copies", 0))
        except (TypeError, ValueError):
            copies = 0

        # -------------------------------------------------
        # BOOK AVAILABLE -> BORROW IMMEDIATELY
        # -------------------------------------------------

        if copies > 0:

            selected_book["copies"] = copies - 1

            new_record = {

                "user_email": current_email,

                "book_id": str(selected_book.get("book_id")),

                "book_name": selected_book.get("book_name"),

                "author": selected_book.get("author"),

                "category": selected_book.get("category"),

                "borrowed_date": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

                "due_date": (
                    datetime.now()
                    + timedelta(days=get_borrow_days())
                ).strftime("%Y-%m-%d %H:%M:%S"),

                "returned": False

            }

            borrow_records.append(new_record)

            save_books(books)
            save_borrow_records(borrow_records)

            add_notification(
                current_email,
                f"You borrowed '{selected_book.get('book_name')}'. "
                f"Due back on "
                f"{new_record['due_date'][:10]}.",
                "borrow_success"
            )

            flash(
                f"'{selected_book.get('book_name')}' was borrowed "
                f"successfully!",
                "success"
            )

        # -------------------------------------------------
        # BOOK NOT AVAILABLE -> AUTOMATICALLY WAITLIST
        # -------------------------------------------------

        else:

            waitlist_entry = {

                "user_email": current_email,

                "user_name": current_name,

                "book_id": str(selected_book.get("book_id")),

                "book_name": selected_book.get("book_name", ""),

                "requested_date": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            }

            waitlist.append(waitlist_entry)

            save_waitlist(waitlist)

            flash(
                f"'{selected_book.get('book_name')}' is currently "
                f"unavailable. You have been added to the waitlist "
                f"and will be notified when it is available.",
                "warning"
            )

        return redirect(url_for("borrow_books"))

    # Only show real book entries
    valid_books = [book for book in books if book.get("book_id")]

    return render_template(
        "borrow_books.html",
        books=valid_books
    )

# =========================================================
# USER - RETURN BOOKS
# =========================================================

@app.route("/return_books", methods=["GET", "POST"])
def return_books():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))

    books = load_books()
    borrow_records = load_borrow_records()

    current_email = session.get("email", "").strip().lower()

    if request.method == "POST":

        book_id = request.form.get("book_id", "").strip()

        if not book_id:

            flash(
                "Please select a book to return.",
                "error"
            )

            return redirect(url_for("return_books"))

        record_found = False
        already_requested = False
        returned_book_name = "the book"

        # Find user's active borrowed book
        for record in borrow_records:

            if not isinstance(record, dict):
                continue

            if (
                str(record.get("user_email", "")).strip().lower()
                == current_email
                and str(record.get("book_id", "")).strip() == book_id
                and record.get("returned") is False
            ):

                record_found = True

                returned_book_name = record.get(
                    "book_name", "the book"
                )

                # A return request has already been submitted for
                # this book - don't let the user submit it again.
                if record.get("return_requested") is True:
                    already_requested = True
                    break

                # -----------------------------------------------
                # DO NOT finalize the return here. Users can no
                # longer complete a return (or pick the book's
                # condition/fine) themselves - that would bypass
                # the librarian's physical book-condition check.
                # We only flag the record as "return requested";
                # the librarian's existing Return Books page (and
                # its existing return/fine logic) is what actually
                # marks the book as returned and assesses any
                # damage/lost fine.
                # -----------------------------------------------

                record["return_requested"] = True

                record["return_requested_date"] = (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )

                break

        if not record_found:

            flash(
                "You have not borrowed this book.",
                "error"
            )

            return redirect(url_for("return_books"))

        if already_requested:

            flash(
                "You've already requested a return for this book. "
                "Please wait for the librarian to confirm it.",
                "error"
            )

            return redirect(url_for("return_books"))

        save_borrow_records(borrow_records)

        add_notification(
            current_email,
            f"Your return request for '{returned_book_name}' has "
            f"been submitted. A librarian will inspect the book "
            f"and complete your return.",
            "return_requested"
        )

        flash(
            "Return request submitted! A librarian will inspect "
            "the book and complete your return.",
            "success"
        )

        return redirect(url_for("return_books"))

    # =====================================================
    # BUILD LIST OF THE USER'S CURRENTLY BORROWED BOOKS
    # =====================================================

    borrowed_books = []

    for record in borrow_records:

        if not isinstance(record, dict):
            continue

        if (
            str(record.get("user_email", "")).strip().lower()
            == current_email
            and record.get("returned") is False
        ):

            borrowed_books.append({
                "book_id": record.get("book_id", ""),
                "book_name": record.get("book_name", "N/A"),
                "author": record.get("author", "N/A"),
                "borrowed_date": record.get("borrowed_date", "N/A"),
                "due_date": record.get("due_date", "N/A"),
                "return_requested": record.get(
                    "return_requested"
                ) is True,
            })

    return render_template(
        "return_books.html",
        borrowed_books=borrowed_books
    )
# =========================================================
# USER - RENEW BOOKS
# =========================================================

@app.route("/renew-books", methods=["GET", "POST"])
def renew_books():

    # =====================================================
    # USER LOGIN CHECK
    # =====================================================

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))

    current_email = session.get("email", "").strip().lower()

    # =====================================================
    # REQUEST A RENEWAL (shared logic - see
    # create_renewal_request()). This inserts a PENDING row into
    # renewal_requests; it never updates due_date itself. A
    # librarian/admin must approve it for the due date to change.
    # =====================================================

    if request.method == "POST":

        record_id = request.form.get("record_id", "").strip()

        if not record_id.isdigit():
            flash("Invalid borrowing record.", "error")
            return redirect(url_for("renew_books"))

        try:
            create_renewal_request(
                record_id=int(record_id),
                acting_user_email=current_email
            )
        except RenewalError as err:
            flash(err.message, "error")
            return redirect(url_for("renew_books"))

        flash("Renewal request submitted. Awaiting librarian approval.", "success")
        return redirect(url_for("renew_books"))

    # =====================================================
    # SHOW ONLY THE LOGGED-IN USER'S OWN ACTIVE BORROWED BOOKS
    # =====================================================

    all_records = load_borrow_records()
    pending_borrow_ids = _load_open_renewal_request_borrow_ids()

    active_records = []

    for record in all_records:

        if not isinstance(record, dict):
            continue

        if record.get("returned") is not False:
            continue

        if str(record.get("user_email", "")).strip().lower() != current_email:
            continue

        record_copy = record.copy()

        due_date_text = record_copy.get("due_date")
        has_pending_request = record_copy.get("id") in pending_borrow_ids

        window = _renewal_window_status(
            due_date_text,
            record_copy.get("returned"),
            record_copy.get("renewal_count"),
            has_pending_request
        )

        record_copy["renew_status"] = window["status_label"]
        record_copy["can_renew"] = window["can_request"]
        record_copy["renewal_pending"] = has_pending_request

        # Days remaining until due date (can be negative if overdue),
        # for the "Days remaining" column on the Renew Books page.
        try:
            due_date = datetime.strptime(due_date_text, "%Y-%m-%d %H:%M:%S")
            record_copy["days_remaining"] = (due_date.date() - datetime.now().date()).days
        except (ValueError, TypeError):
            record_copy["days_remaining"] = None

        active_records.append(record_copy)

    return render_template(
        "renew_books.html",
        borrowed_books=active_records
    )


# =========================================================
# USER - RESERVE BOOKS
# =========================================================
# =========================================================
# USER - RESERVE BOOK
# =========================================================
@app.route("/reserve-books/<book_id>", methods=["POST"])
def reserve_books(book_id):

    # =====================================================
    # CHECK LOGIN
    # =====================================================

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))


    # =====================================================
    # LOAD DATA
    # =====================================================

    books = load_books()
    waitlist = load_waitlist()
    users = load_users()


    # =====================================================
    # GET CURRENT USER
    # =====================================================

    current_email = session.get(
        "email",
        ""
    ).strip().lower()

    current_user = None

    for user in users:

        if (
            user.get("email", "").strip().lower()
            == current_email
        ):

            current_user = user
            break


    # =====================================================
    # FIND BOOK
    # =====================================================

    book_id = str(book_id).strip()

    selected_book = None

    for book in books:

        if (
            str(
                book.get("book_id", "")
            ).strip()
            == book_id
        ):

            selected_book = book
            break


    # =====================================================
    # BOOK NOT FOUND
    # =====================================================

    if selected_book is None:

        flash(
            "Book not found.",
            "error"
        )

        return redirect(
            url_for("view_books")
        )


    # =====================================================
    # CHECK AVAILABLE COPIES
    # =====================================================

    try:

        copies = int(
            selected_book.get(
                "copies",
                0
            )
        )

    except (TypeError, ValueError):

        copies = 0


    # =====================================================
    # BOOK IS AVAILABLE
    # =====================================================

    if copies > 0:

        flash(
            f"'{selected_book.get('book_name', 'Book')}' "
            f"is currently available. Please ask the librarian "
            f"to issue the book to you.",
            "warning"
        )

        return redirect(
            url_for("view_books")
        )


    # =====================================================
    # CHECK WHETHER ALREADY BORROWED
    # =====================================================

    borrow_records = load_borrow_records()

    for record in borrow_records:

        if not isinstance(
            record,
            dict
        ):
            continue

        record_email = record.get(
            "user_email",
            ""
        )

        if not isinstance(
            record_email,
            str
        ):
            record_email = str(
                record_email
            )

        record_book_id = str(
            record.get(
                "book_id",
                ""
            )
        ).strip()

        returned = record.get(
            "returned"
        )

        if (
            record_email.strip().lower()
            == current_email
            and record_book_id
            == book_id
            and returned is False
        ):

            flash(
                "You already have this book.",
                "error"
            )

            return redirect(
                url_for("view_books")
            )


    # =====================================================
    # CHECK DUPLICATE WAITLIST ENTRY
    # =====================================================

    for waiting_user in waitlist:

        if not isinstance(
            waiting_user,
            dict
        ):
            continue

        waiting_email = waiting_user.get(
            "user_email",
            ""
        )

        if not isinstance(
            waiting_email,
            str
        ):
            waiting_email = str(
                waiting_email
            )

        waiting_book_id = str(
            waiting_user.get(
                "book_id",
                ""
            )
        ).strip()

        if (
            waiting_email.strip().lower()
            == current_email
            and waiting_book_id
            == book_id
        ):

            flash(
                "You are already on the waitlist for "
                "this book.",
                "warning"
            )

            return redirect(
                url_for("view_books")
            )


    # =====================================================
    # GET USER NAME
    # =====================================================

    user_name = session.get(
        "name",
        current_email
    )

    if current_user is not None:

        user_name = current_user.get(
            "name",
            user_name
        )


    # =====================================================
    # CREATE WAITLIST ENTRY
    # =====================================================

    waitlist_entry = {

        "user_email":
            current_email,

        "user_name":
            user_name,

        "book_id":
            str(
                selected_book.get(
                    "book_id"
                )
            ),

        "book_name":
            selected_book.get(
                "book_name",
                ""
            ),

        "requested_date":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    }


    # =====================================================
    # ADD TO WAITLIST
    # =====================================================

    waitlist.append(
        waitlist_entry
    )


    # =====================================================
    # SAVE WAITLIST
    # =====================================================

    save_waitlist(
        waitlist
    )


    # =====================================================
    # SUCCESS MESSAGE
    # =====================================================

    flash(
        f"'{selected_book.get('book_name', 'Book')}' "
        f"has been reserved successfully. "
        f"You are now in the waiting list.",
        "success"
    )


    # =====================================================
    # RETURN TO VIEW BOOKS
    # =====================================================

    return redirect(
        url_for("view_books")
    )
# =========================================================
# USER - BORROW HISTORY
# =========================================================

@app.route("/borrow_history")
def borrow_history():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))

    user_email = session.get("email")

    # Read borrow records
    borrow_records = load_borrow_records()

    # Only show records belonging to the logged-in user
    user_records = [
        record
        for record in borrow_records
        if record
        and record.get("user_email")
        and record.get("user_email").lower() == user_email.lower()
    ]

    return render_template(
        "borrow_history.html",
        borrow_records=user_records
    )

# =========================================================
# BACK TO CORRESPONDING DASHBOARD
# =========================================================

@app.route("/back-to-dashboard")
def back_to_dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    role = session.get("role")

    if role == "admin":
        return redirect(url_for("admin_dashboard"))

    if role == "librarian":
        return redirect(url_for("librarian_dashboard"))

    if role == "user":
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# =========================================================
# GO DASHBOARD
# =========================================================

@app.route("/go-dashboard")
def go_dashboard():

    return back_to_dashboard()


# =========================================================
# REGISTER
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # =====================================================
        # SECURITY: the public /register route creates ONLY "user"
        # accounts. There is no role field on register.html anymore,
        # but even if a request is hand-crafted (curl/Postman/edited
        # HTML) with role=librarian or role=admin in the POST body,
        # it is never read here - role is a fixed, hardcoded literal,
        # not something derived from request.form. Librarian and
        # Admin accounts can only be created by an existing Admin via
        # /create-account (see create_account()), which is guarded by
        # its own session role check.
        # =====================================================
        role = "user"

        if not name or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        password_error = validate_password_policy(password)

        if password_error:
            flash(password_error, "error")
            return redirect(url_for("register"))

        users = load_users()

        for user in users:

            if user.get("email", "").strip().lower() == email:
                flash("Email already exists.", "error")
                return redirect(url_for("register"))

        hashed_password = hash_password(password)

        add_user(name, email, hashed_password, role)

        flash("Registration Successful!", "success")

        return redirect(url_for("login"))

    return render_template("register.html")

# =========================================================
# LOGIN
# =========================================================

# =========================================================
# LOGIN - LIGHTWEIGHT RATE LIMITING
#
# A simple in-memory counter (per email) that locks an account out
# of login attempts for a short cooldown after too many wrong
# passwords in a row - without adding a new database table or
# external dependency. This resets if the app restarts and is
# per-process only, which is an acceptable, honestly-documented
# limitation for this project's scale; it still closes the
# "unlimited password guesses" gap for normal use.
# =========================================================

LOGIN_ATTEMPT_LIMIT = 5
LOGIN_LOCKOUT_MINUTES = 5

_login_attempts = {}


def _is_login_locked(email):
    entry = _login_attempts.get(email)
    if not entry:
        return False
    locked_until = entry.get("locked_until")
    if locked_until and datetime.now() < locked_until:
        return True
    if locked_until and datetime.now() >= locked_until:
        _login_attempts.pop(email, None)
    return False


def _register_failed_login(email):
    entry = _login_attempts.setdefault(email, {"count": 0, "locked_until": None})
    entry["count"] += 1
    if entry["count"] >= LOGIN_ATTEMPT_LIMIT:
        entry["locked_until"] = datetime.now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)


def _clear_login_attempts(email):
    _login_attempts.pop(email, None)


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if _is_login_locked(email):
            flash(
                "Too many failed login attempts. Please try again "
                "in a few minutes.",
                "error"
            )
            return render_template("login.html")

        user = find_user(email)

        if user is None:
            _register_failed_login(email)
            flash("Invalid Email or Password", "error")
            return render_template("login.html")

        stored_password = user.get("password", "")

        try:
            password_match = check_password(
                stored_password,
                password
            )
        except Exception:
            password_match = False

        if not password_match:
            _register_failed_login(email)
            flash("Invalid Email or Password", "error")
            return render_template("login.html")

        user_role = user.get("role", "user").lower()

        _clear_login_attempts(email)

        # Store the authenticated account and its role from the database.
        # The role is never taken from a login-form selection.
        session["email"] = user.get("email")
        session["name"] = user.get("name")
        session["role"] = user_role

        # Direct login: send every account straight to its own dashboard.
        if user_role == "user":
            return redirect(url_for("dashboard"))
        elif user_role == "librarian":
            return redirect(url_for("librarian_dashboard"))
        elif user_role == "admin":
            return redirect(url_for("admin_dashboard"))

        session.clear()
        flash("Invalid account role.", "error")
        return redirect(url_for("login"))

    return render_template("login.html")


# =========================================================
# USER DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "user":
        return redirect(url_for("login"))

    return render_template(
        "user_dashboard.html",
        name=session.get("name", "User")
    )


# =========================================================
# LIBRARIAN DASHBOARD
# =========================================================

@app.route("/librarian-dashboard")
def librarian_dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "librarian":
        return redirect(url_for("login"))

    report_stats = compute_report_stats()

    return render_template(
        "librarian_dashboard.html",
        name=session.get("name", "Librarian"),
        report_stats=report_stats
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin-dashboard")
def admin_dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    report_stats = compute_report_stats()

    return render_template(
        "admin_dashboard.html",
        name=session.get("name", "Admin"),
        report_stats=report_stats
    )


# =========================================================
# NOTIFICATIONS
# =========================================================

@app.route("/notifications", methods=["GET", "POST"])
def notifications():

    if "email" not in session:
        return redirect(url_for("login"))

    current_email = session.get("email", "").strip().lower()

    all_notifications = load_notifications()

    if request.method == "POST":

        action = request.form.get("action", "")

        if action == "mark_all_read":

            for n in all_notifications:

                if (
                    isinstance(n, dict)
                    and n.get("user_email", "").strip().lower()
                    == current_email
                ):
                    n["read"] = True

            save_notifications(all_notifications)

            flash("All notifications marked as read.", "success")

        elif action == "mark_read":

            notif_id = request.form.get("notification_id", "")

            for n in all_notifications:

                if (
                    isinstance(n, dict)
                    and str(n.get("id")) == str(notif_id)
                    and n.get("user_email", "").strip().lower()
                    == current_email
                ):
                    n["read"] = True
                    break

            save_notifications(all_notifications)

        return redirect(url_for("notifications"))

    # Only this user's own notifications - never another user's
    my_notifications = [
        n for n in all_notifications
        if isinstance(n, dict)
        and n.get("user_email", "").strip().lower() == current_email
    ]

    # Most recent first
    my_notifications = sorted(
        my_notifications,
        key=lambda n: n.get("id", 0),
        reverse=True
    )

    return render_template(
        "notifications.html",
        notifications=my_notifications
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# =========================================================
# PROFILE
# =========================================================

@app.route("/profile", methods=["GET", "POST"])
def profile():

    if "email" not in session:
        return redirect(url_for("login"))

    users = load_users()

    current_user = None

    for user in users:

        if user.get("email", "").strip().lower() == session.get("email", "").strip().lower():
            current_user = user
            break

    if current_user is None:

        session.clear()

        return redirect(url_for("login"))

    if request.method == "POST":

        new_name = request.form.get("name", "").strip()

        if not new_name:
            flash("Name cannot be empty.", "error")
            return redirect(url_for("profile"))

        current_user["name"] = new_name

        session["name"] = new_name

        save_users(users)

        flash("Profile Updated Successfully.", "success")

        return redirect(url_for("profile"))

    return render_template(
        "profile.html",
        user=current_user
    )


# =========================================================
# CHANGE PASSWORD
# =========================================================

@app.route("/change-password", methods=["GET", "POST"])
def change_password():

    if "email" not in session:
        return redirect(url_for("login"))

    users = load_users()

    current_user = None

    for user in users:

        if user.get("email", "").strip().lower() == session.get("email", "").strip().lower():
            current_user = user
            break

    if current_user is None:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":

        old_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password(
            current_user["password"],
            old_password
        ):
            flash("Old Password is Incorrect.", "error")
            return redirect(url_for("change_password"))

        if new_password != confirm_password:
            flash(
                "New Password and Confirm Password do not match.",
                "error"
            )
            return redirect(url_for("change_password"))

        password_error = validate_password_policy(new_password)

        if password_error:
            flash(password_error, "error")
            return redirect(url_for("change_password"))

        if check_password(
            current_user["password"],
            new_password
        ):
            flash(
                "New Password cannot be the same as the current password.",
                "error"
            )
            return redirect(url_for("change_password"))

        set_user_password(
            current_user["email"],
            hash_password(new_password)
        )

        flash("Password Changed Successfully.", "success")

        return redirect(url_for("back_to_dashboard"))

    return render_template("change_password.html")


# =========================================================
# FORGOT PASSWORD  (step 1: request an OTP by email)
# =========================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        if not email or len(email) > 254:
            flash("Please enter your registered email address.", "error")
            return redirect(url_for("forgot_password"))

        # Checked before looking at the account, so it says nothing about
        # which emails are registered. Without Gmail credentials no OTP
        # can ever be delivered.
        if not _mail_is_configured():
            app.logger.error(
                "Forgot Password: MAIL_USERNAME / MAIL_PASSWORD are not "
                "set. Put them in a .env file next to app.py (see "
                ".env.example) or in the environment, then restart."
            )
            flash(
                "Email service is not configured, so the OTP cannot be "
                "sent. Set " + " and ".join(_mail_missing_settings())
                + " in the .env file next to app.py "
                "and restart the application.",
                "error"
            )
            return redirect(url_for("forgot_password"))

        user = find_user(email)

        # Any earlier reset attempt in this browser session is dropped.
        _clear_reset_session()

        # Only a registered account can request an OTP. The recipient is
        # always the email stored on that verified database account.
        session["reset_email"] = email
        session["reset_id"] = 0
        session["otp_attempts"] = 0

        if user is None:
            flash("No account found with this email address.", "error")
            _clear_reset_session()
            return redirect(url_for("forgot_password"))

        if user is not None:

            # The OTP always goes to the address stored on the account.
            recipient_email = user.get("email", "").strip().lower() or email

            otp = generate_otp()
            now = datetime.now()

            conn = get_db_connection()

            try:
                # Generating a new OTP invalidates every earlier one for
                # this account.
                conn.execute(
                    "UPDATE password_reset_otps SET used = 1, otp_hash = '' "
                    "WHERE email = ? AND used = 0",
                    (recipient_email,)
                )

                # Housekeeping: drop reset records older than a day.
                conn.execute(
                    "DELETE FROM password_reset_otps WHERE created_at < ?",
                    ((now - timedelta(days=1)).strftime(_RESET_DT_FORMAT),)
                )

                cur = conn.execute(
                    "INSERT INTO password_reset_otps "
                    "(email, otp_hash, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        recipient_email,
                        bcrypt.generate_password_hash(otp).decode("utf-8"),
                        now.strftime(_RESET_DT_FORMAT),
                        (now + timedelta(minutes=OTP_VALIDITY_MINUTES))
                        .strftime(_RESET_DT_FORMAT)
                    )
                )

                reset_id = cur.lastrowid
                conn.commit()

                sent, send_error = _send_otp_email(recipient_email, otp)

                if sent:
                    session["reset_email"] = recipient_email
                    session["reset_id"] = reset_id
                else:
                    # Nothing was delivered, so the OTP is useless.
                    _invalidate_reset_row(conn, reset_id)
                    _clear_reset_session()
                    flash(send_error, "error")
                    return redirect(url_for("forgot_password"))

            finally:
                conn.close()

        flash(
            "A 6-digit OTP has been sent to your registered email address.",
            "success"
        )

        return redirect(url_for("verify_otp"))

    return render_template("forgot_password.html")


# =========================================================
# VERIFY OTP  (step 2: check the emailed OTP)
# =========================================================

@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():

    if "reset_id" not in session or not session.get("reset_email"):
        if request.method == "POST":
            flash("OTP has expired. Please request a new OTP.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "GET":

        conn = get_db_connection()

        try:
            row = _get_reset_row(conn)
        finally:
            conn.close()

        # Already verified in this session - carry on to the next step.
        if row is not None and row["verified"] and not row["used"]:
            return redirect(url_for("reset_password"))

        return render_template("verify_otp.html")

    entered_otp = request.form.get("otp", "").strip()

    conn = get_db_connection()

    try:

        row = _get_reset_row(conn)

        # Unknown email, or a stale/forged session: behave exactly like
        # a wrong OTP.
        if row is None:

            attempts = session.get("otp_attempts", 0) + 1

            if attempts >= OTP_MAX_ATTEMPTS:
                _clear_reset_session()
                flash(
                    "Too many incorrect attempts. Please request a new "
                    "OTP.",
                    "error"
                )
                return redirect(url_for("forgot_password"))

            session["otp_attempts"] = attempts
            flash("Invalid OTP. Please try again.", "error")
            return redirect(url_for("verify_otp"))

        # Already verified in this session (e.g. a double-submitted
        # form): the OTP itself is never checked again, simply carry
        # on to the reset step.
        if row["verified"] and not row["used"]:
            return redirect(url_for("reset_password"))

        expires_at = _parse_reset_dt(row["expires_at"])

        # Expired, already used or superseded.
        if (row["used"] or expires_at is None
                or datetime.now() > expires_at):

            if not row["used"]:
                _invalidate_reset_row(conn, row["id"])

            _clear_reset_session()
            flash("OTP has expired. Please request a new OTP.", "error")
            return redirect(url_for("forgot_password"))

        otp_ok = False

        if re.fullmatch(r"[0-9]{6}", entered_otp):
            try:
                otp_ok = bcrypt.check_password_hash(
                    row["otp_hash"], entered_otp
                )
            except ValueError:
                otp_ok = False

        if not otp_ok:

            attempts = row["attempts"] + 1

            if attempts >= OTP_MAX_ATTEMPTS:
                _invalidate_reset_row(conn, row["id"])
                _clear_reset_session()
                flash(
                    "Too many incorrect attempts. Please request a new "
                    "OTP.",
                    "error"
                )
                return redirect(url_for("forgot_password"))

            conn.execute(
                "UPDATE password_reset_otps SET attempts = ? WHERE id = ?",
                (attempts, row["id"])
            )
            conn.commit()

            flash("Invalid OTP. Please try again.", "error")
            return redirect(url_for("verify_otp"))

        # Correct OTP: mark this reset request as verified and discard
        # the OTP hash so the OTP can never be checked again.
        conn.execute(
            "UPDATE password_reset_otps SET verified = 1, otp_hash = '', "
            "reset_expires_at = ? WHERE id = ?",
            (
                (datetime.now()
                 + timedelta(minutes=OTP_RESET_WINDOW_MINUTES))
                .strftime(_RESET_DT_FORMAT),
                row["id"]
            )
        )
        conn.commit()

    finally:
        conn.close()

    flash("OTP verified. Please set your new password.", "success")

    return redirect(url_for("reset_password"))


# =========================================================
# RESET PASSWORD  (step 3: only reachable after OTP verification)
# =========================================================

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():

    conn = get_db_connection()

    try:

        row = _get_reset_row(conn)

        if row is None or row["used"]:
            _clear_reset_session()
            flash(
                "Please request an OTP to reset your password.",
                "error"
            )
            return redirect(url_for("forgot_password"))

        if not row["verified"]:
            flash(
                "Please verify the OTP sent to your email first.",
                "error"
            )
            return redirect(url_for("verify_otp"))

        reset_expires_at = _parse_reset_dt(row["reset_expires_at"])

        if reset_expires_at is None or datetime.now() > reset_expires_at:
            _invalidate_reset_row(conn, row["id"])
            _clear_reset_session()
            flash(
                "Your password reset session has expired. Please "
                "request a new OTP.",
                "error"
            )
            return redirect(url_for("forgot_password"))

        if request.method == "POST":

            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if new_password != confirm_password:
                flash("Passwords do not match.", "error")
                return redirect(url_for("reset_password"))

            password_error = validate_password_policy(new_password)

            if password_error:
                flash(password_error, "error")
                return redirect(url_for("reset_password"))

            hashed_password = hash_password(new_password)

            updated = conn.execute(
                "UPDATE users SET password = ? "
                "WHERE lower(trim(email)) = ?",
                (hashed_password, row["email"])
            ).rowcount

            # The OTP request is consumed whether or not the account
            # still exists, so it can never be reused.
            conn.execute(
                "UPDATE password_reset_otps SET used = 1, otp_hash = '' "
                "WHERE id = ?",
                (row["id"],)
            )
            conn.commit()

            _clear_reset_session()

            if not updated:
                flash("User account not found.", "error")
                return redirect(url_for("forgot_password"))

            flash(
                "Password reset successful. Please log in with your "
                "new password.",
                "success"
            )

            return redirect(url_for("login"))

    finally:
        conn.close()

    return render_template("reset_password.html")


# =========================================================
# PRINT ROUTES
# =========================================================

print("\n========== FLASK ROUTES ==========")

for rule in app.url_map.iter_rules():
    print(rule)

print("==================================\n")


LATE_FINE_PER_DAY = 50
DAMAGE_FINE = 100
LOST_FINE = 1000




def load_books():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT book_id, book_name, author, publisher, isbn, category, copies "
        "FROM books ORDER BY seq"
    )
    rows = cur.fetchall()
    conn.close()
    db.remember_snapshot("books", rows)
    return [dict(row) for row in rows]


def save_books(books):
    db.sync_table("books", [
        (
            b.get("book_id"),
            b.get("book_name"),
            b.get("author"),
            b.get("publisher"),
            b.get("isbn"),
            b.get("category"),
            b.get("copies"),
        )
        for b in books
    ])


def load_borrow_records():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {db.qcols(BORROW_RECORD_COLUMNS)} "
        f"FROM borrow_records ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()
    db.remember_snapshot("borrow_records", rows)

    records = []
    for row in rows:
        d = dict(row)
        if d.get("returned") is not None:
            d["returned"] = bool(d["returned"])
        if d.get("return_requested") is not None:
            d["return_requested"] = bool(d["return_requested"])
        records.append(d)

    return records


def save_borrow_records(records):
    """
    Saves the borrow-record list back to the database.

    A record's "id" is its permanent identifier (renewal_requests link to
    it). Existing records keep their id; a brand-new record (no "id" yet)
    gets one from the database and it is written back into that dict.
    Only records that were added, changed or removed are written - the
    table is never wiped and rewritten, so one person's save cannot
    erase another person's simultaneous change.
    """

    rows = []
    for r in records:
        values = []
        for col in BORROW_RECORD_COLUMNS:
            val = r.get(col)
            if col in ("returned", "return_requested") and val is not None:
                val = int(bool(val))
            elif col == "renewal_count" and val is None:
                # NOT NULL column - a record with no renewal_count
                # yet (e.g. freshly created by borrow_books()) starts
                # a new transaction with 0 renewals used.
                val = 0
            values.append(val)
        rows.append(tuple(values))

    assigned = db.sync_table("borrow_records", rows)

    for index, new_id in assigned.items():
        records[index]["id"] = new_id


# =========================================================
# RENEW BOOKS - SHARED RENEWAL LOGIC
#
# Renewal is no longer a direct, self-service update of
# borrow_records.due_date. A user instead SUBMITS A RENEWAL REQUEST
# (inserted into the renewal_requests table as PENDING), which a
# librarian/admin must approve or reject. Approving is what actually
# extends the due date; rejecting leaves it untouched. Every rule
# below is enforced here, server-side - never only by hiding/showing
# a button in the templates - so calling the underlying API directly
# is just as constrained as clicking through the UI.
# =========================================================

class RenewalError(Exception):
    """
    Raised when a renewal request/approval/rejection cannot be
    completed. `message` is the exact user-facing flash text the
    calling route should show.
    """

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _renewal_window_status(due_date_text, returned, renewal_count, has_pending_request):
    """
    Computes, for a single active borrow record, whether a renewal
    request can be submitted right now, and the display label to
    show for it.

    The rule (server-side, and the ONLY place it is decided):
    renewal requests are accepted only on the single calendar day
    immediately before the due date - not right after borrowing, and
    not once the due date has passed.

        due_date - 1 day  -> can request
        any earlier day   -> too early
        due date itself   -> too late (must return/renew already happened)
        past due date     -> overdue, too late

    Returns a dict: is_overdue, can_request, status_label.
    """

    try:
        due_date = datetime.strptime(due_date_text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return {
            "is_overdue": False,
            "can_request": False,
            "status_label": "Due date unavailable",
        }

    today = datetime.now().date()
    due_date_only = due_date.date()
    renewal_opens_on = due_date_only - timedelta(days=1)

    is_overdue = today > due_date_only

    if returned:
        status_label = "Returned"
        can_request = False
    elif is_overdue:
        status_label = "Overdue"
        can_request = False
    elif has_pending_request:
        status_label = "Renewal Request Pending"
        can_request = False
    elif int(renewal_count or 0) >= 1:
        status_label = "Already renewed"
        can_request = False
    elif today == renewal_opens_on:
        status_label = "Available for renewal"
        can_request = True
    elif today == due_date_only:
        status_label = "Too late to request renewal"
        can_request = False
    else:
        status_label = "Not yet eligible for renewal"
        can_request = False

    return {
        "is_overdue": is_overdue,
        "can_request": can_request,
        "status_label": status_label,
    }


def _load_open_renewal_request_borrow_ids():
    """
    Set of borrow_id values that currently have a PENDING renewal
    request, used to stop a user from submitting a second request
    for the same borrowing while one is still awaiting review.
    """

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT borrow_id FROM renewal_requests WHERE status = 'PENDING'"
    )
    ids = {row["borrow_id"] for row in cur.fetchall()}
    conn.close()
    return ids


def create_renewal_request(record_id, acting_user_email):
    """
    Validates and inserts a new renewal request for record_id on
    behalf of acting_user_email. This is the ONLY way a renewal now
    starts - it never touches borrow_records.due_date itself; that
    only happens when a librarian/admin later approves the request
    (see approve_renewal_request()).

    Server-side checks, in order (never trust the frontend button):
      1. record_id parses to a real integer.
      2. The borrow record exists.
      3. The book belongs to this user.
      4. The book has not already been returned.
      5. The book is not already overdue.
      6. Today is exactly the single day before the due date.
      7. No renewal has already been used for this borrowing.
      8. No renewal request is already PENDING/APPROVED for it.

    Raises RenewalError (ready-to-flash message) on any failure.
    Returns the new renewal_request_id on success.
    """

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise RenewalError("Invalid borrowing record.")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, user_email, book_id, book_name, due_date, returned, "
        "renewal_count FROM borrow_records WHERE id = ?",
        (record_id,)
    )
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise RenewalError("Borrowing record not found.")

    record = dict(row)

    owner_email = (record.get("user_email") or "").strip().lower()
    looked_up_email = (acting_user_email or "").strip().lower()
    if not looked_up_email or owner_email != looked_up_email:
        conn.close()
        # Same generic message as "not found" - never confirm to a
        # user that a different user's record exists at all.
        raise RenewalError("Borrowing record not found.")

    if bool(record.get("returned")):
        conn.close()
        raise RenewalError(
            "This book has already been returned and cannot be renewed."
        )

    if int(record.get("renewal_count") or 0) >= 1:
        conn.close()
        raise RenewalError("This book has already been renewed once.")

    cur.execute(
        "SELECT renewal_request_id FROM renewal_requests "
        "WHERE borrow_id = ? AND status = 'PENDING'",
        (record_id,)
    )
    if cur.fetchone() is not None:
        conn.close()
        raise RenewalError(
            "A renewal request for this book is already pending review."
        )

    due_date_text = record.get("due_date")

    try:
        current_due_date = datetime.strptime(due_date_text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        conn.close()
        raise RenewalError("This borrowing record has an invalid due date.")

    today = datetime.now().date()
    due_date_only = current_due_date.date()
    renewal_opens_on = due_date_only - timedelta(days=1)

    if today > due_date_only:
        conn.close()
        raise RenewalError(
            "This book is overdue. Renewal requests are no longer "
            "accepted - please return the book."
        )

    if today != renewal_opens_on:
        conn.close()
        raise RenewalError(
            "Renewal requests can only be submitted on the day "
            "before the due date."
        )

    requested_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    requested_due_date = (
        current_due_date + timedelta(days=get_renewal_days())
    ).strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "INSERT INTO renewal_requests "
        "(borrow_id, user_email, book_id, requested_at, "
        "requested_due_date, status) VALUES (?, ?, ?, ?, ?, 'PENDING')",
        (
            record_id,
            record.get("user_email"),
            record.get("book_id"),
            requested_at_text,
            requested_due_date,
        )
    )
    new_id = cur.lastrowid

    conn.commit()
    conn.close()

    add_notification(
        record.get("user_email", ""),
        f"Your renewal request for '{record.get('book_name', 'Book')}' "
        f"has been submitted and is awaiting librarian approval.",
        "renewal_requested",
        extra={"title": "Renewal Requested", "book_name": record.get("book_name")}
    )

    return new_id


def approve_renewal_request(renewal_request_id, librarian_email):
    """
    Approves a PENDING renewal request: extends the linked borrow
    record's due date by get_renewal_days(), marks the borrowing as
    renewed (renewal_count + 1), and marks the request APPROVED.

    Re-validates the underlying borrow record at approval time (not
    already returned) so a book returned while the request was
    pending cannot still be "renewed".
    """

    try:
        renewal_request_id = int(renewal_request_id)
    except (TypeError, ValueError):
        raise RenewalError("Invalid renewal request.")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT renewal_request_id, borrow_id, user_email, status "
        "FROM renewal_requests WHERE renewal_request_id = ?",
        (renewal_request_id,)
    )
    req_row = cur.fetchone()

    if req_row is None:
        conn.close()
        raise RenewalError("Renewal request not found.")

    req = dict(req_row)

    if req.get("status") != "PENDING":
        conn.close()
        raise RenewalError("This renewal request has already been reviewed.")

    cur.execute(
        "SELECT id, book_name, due_date, returned, renewal_count "
        "FROM borrow_records WHERE id = ?",
        (req.get("borrow_id"),)
    )
    borrow_row = cur.fetchone()

    if borrow_row is None:
        conn.close()
        raise RenewalError("The related borrowing record no longer exists.")

    borrow_record = dict(borrow_row)

    if bool(borrow_record.get("returned")):
        conn.close()
        raise RenewalError(
            "This book has already been returned - the renewal request "
            "can no longer be approved."
        )

    due_date_text = borrow_record.get("due_date")
    try:
        current_due_date = datetime.strptime(due_date_text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        conn.close()
        raise RenewalError("This borrowing record has an invalid due date.")

    new_due_date = current_due_date + timedelta(days=get_renewal_days())
    new_due_date_text = new_due_date.strftime("%Y-%m-%d %H:%M:%S")
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "UPDATE borrow_records "
        "SET due_date = ?, renewal_count = renewal_count + 1, "
        "last_renewed_at = ? "
        "WHERE id = ? AND returned = 0",
        (new_due_date_text, now_text, req.get("borrow_id"))
    )

    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        raise RenewalError(
            "This book could not be renewed - it may have just been "
            "returned. Please refresh and try again."
        )

    cur.execute(
        "UPDATE renewal_requests "
        "SET status = 'APPROVED', librarian_email = ?, reviewed_at = ? "
        "WHERE renewal_request_id = ? AND status = 'PENDING'",
        (librarian_email, now_text, renewal_request_id)
    )

    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        raise RenewalError(
            "This renewal request was just reviewed by someone else. "
            "Please refresh and try again."
        )

    conn.commit()
    conn.close()

    book_name = borrow_record.get("book_name", "Book")
    add_notification(
        req.get("user_email", ""),
        f"Your renewal request for '{book_name}' has been approved. "
        f"Your new due date is {new_due_date.strftime('%d-%m-%Y')}.",
        "renew_success",
        extra={"title": "Renewal Approved", "book_name": book_name}
    )

    return {
        "renewal_request_id": renewal_request_id,
        "borrow_id": req.get("borrow_id"),
        "due_date": new_due_date_text,
    }


def reject_renewal_request(renewal_request_id, librarian_email, remarks=None):
    """
    Rejects a PENDING renewal request: leaves the due date untouched
    and marks the request REJECTED.
    """

    try:
        renewal_request_id = int(renewal_request_id)
    except (TypeError, ValueError):
        raise RenewalError("Invalid renewal request.")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT renewal_request_id, borrow_id, user_email, status "
        "FROM renewal_requests WHERE renewal_request_id = ?",
        (renewal_request_id,)
    )
    req_row = cur.fetchone()

    if req_row is None:
        conn.close()
        raise RenewalError("Renewal request not found.")

    req = dict(req_row)

    if req.get("status") != "PENDING":
        conn.close()
        raise RenewalError("This renewal request has already been reviewed.")

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "UPDATE renewal_requests "
        "SET status = 'REJECTED', librarian_email = ?, reviewed_at = ?, "
        "remarks = ? "
        "WHERE renewal_request_id = ? AND status = 'PENDING'",
        (librarian_email, now_text, remarks, renewal_request_id)
    )

    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        raise RenewalError(
            "This renewal request was just reviewed by someone else. "
            "Please refresh and try again."
        )

    cur.execute(
        "SELECT book_name FROM borrow_records WHERE id = ?",
        (req.get("borrow_id"),)
    )
    book_row = cur.fetchone()
    book_name = book_row["book_name"] if book_row else "Book"

    conn.commit()
    conn.close()

    add_notification(
        req.get("user_email", ""),
        f"Your renewal request for '{book_name}' was rejected by the "
        f"librarian. Please return the book by its current due date.",
        "renewal_rejected",
        extra={"title": "Renewal Rejected", "book_name": book_name}
    )

    return {"renewal_request_id": renewal_request_id}


def load_staff_renewal_requests(status="PENDING"):
    """
    Renewal requests for the librarian/admin dashboard, joined with
    the underlying borrow record and the requester's display name.
    Ordered newest-first. Pass status=None for every request
    regardless of status.
    """

    conn = get_db_connection()
    cur = conn.cursor()

    query = (
        "SELECT rr.renewal_request_id, rr.borrow_id, rr.user_email, "
        "rr.book_id, rr.requested_at, rr.requested_due_date, "
        "rr.status, rr.librarian_email, rr.reviewed_at, rr.remarks, "
        "br.book_name, br.author, br.borrowed_date, br.due_date "
        "FROM renewal_requests rr "
        "LEFT JOIN borrow_records br ON br.id = rr.borrow_id "
    )
    params = []
    if status:
        query += "WHERE rr.status = ? "
        params.append(status)
    query += "ORDER BY rr.requested_at DESC"

    cur.execute(query, params)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    users_by_email = {
        (u.get("email") or "").strip().lower(): u
        for u in load_users()
    }

    for row in rows:
        owner_email = (row.get("user_email") or "").strip().lower()
        owner = users_by_email.get(owner_email)
        row["user_name"] = owner.get("name") if owner else owner_email

    return rows


def load_waitlist():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, {db.qcols(WAITLIST_COLUMNS)} FROM waitlist ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()
    db.remember_snapshot("waitlist", rows)
    return [
        {col: row[col] for col in WAITLIST_COLUMNS}
        for row in rows
    ]


def save_waitlist(waitlist):
    db.sync_table("waitlist", [
        tuple(w.get(col) for col in WAITLIST_COLUMNS)
        for w in waitlist
    ])


# =========================================================
# NOTIFICATIONS
# =========================================================

def load_notifications():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {db.qcols(NOTIFICATION_COLUMNS)} "
        f"FROM notifications ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()
    db.remember_snapshot("notifications", rows)

    notifications = []
    for row in rows:
        d = dict(row)
        if d.get("read") is not None:
            d["read"] = bool(d["read"])
        notifications.append(d)

    return notifications


def save_notifications(notifications):
    rows = []
    for n in notifications:
        values = []
        for col in NOTIFICATION_COLUMNS:
            val = n.get(col)
            if col == "read" and val is not None:
                val = int(bool(val))
            values.append(val)
        rows.append(tuple(values))

    assigned = db.sync_table("notifications", rows)

    for index, new_id in assigned.items():
        notifications[index]["id"] = new_id


def add_notification(user_email, message, notif_type="info", extra=None):
    """
    Create a real notification for a specific user. Only that
    user will ever see it (filtered by exact email match when
    displayed).

    `extra` is an optional dict of additional fields to store on
    the notification record (e.g. fine amount, book title, days
    late). This keeps the richer fine-notification data attached
    to the notification itself, so it stays accurate later even
    if the underlying borrow record changes.

    It inserts ONE row and lets the database assign the id, so two
    people triggering notifications at the same moment can never get
    the same id or overwrite each other's notifications.
    """

    if not user_email:
        return

    entry = {
        "user_email": user_email.strip().lower(),
        "message": message,
        "type": notif_type,
        "created_date": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "read": False
    }

    if isinstance(extra, dict):
        entry.update(extra)

    columns = [
        col for col in NOTIFICATION_COLUMNS
        if col != "id" and col in entry
    ]
    values = []
    for col in columns:
        val = entry.get(col)
        if col == "read" and val is not None:
            val = int(bool(val))
        values.append(val)

    conn = get_db_connection()
    try:
        conn.execute(
            f"INSERT INTO notifications ({db.qcols(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values)
        )
        conn.commit()
    finally:
        conn.close()


def build_fine_notification(record, fine_data, condition):
    """
    Build the title/message/extra payload for a "Fine Issued"
    notification, using ONLY numbers already produced by the
    project's existing calculate_fine()/calculate_late_days()
    logic (fine_data). This never recalculates a fine on its own.

    Returns None if there is nothing to notify about (no fine).
    """

    total_fine = fine_data.get("total_fine", 0)

    if not total_fine or total_fine <= 0:
        return None

    days_late = fine_data.get("days_late", 0) or 0
    book_name = record.get("book_name", "the book")

    day_word = "day" if days_late == 1 else "days"

    reasons = []

    if days_late > 0:
        reasons.append(f"returned {days_late} {day_word} late")

    if condition == "damaged":
        reasons.append("damaged")

    elif condition == "lost":
        reasons.append("reported lost")

    if not reasons:
        # Fine exists but doesn't map to a known reason - fall back
        # to a generic phrase rather than showing nothing.
        reason_text = "a fine was applied"
    elif len(reasons) == 1:
        reason_text = reasons[0]
    else:
        reason_text = f"{reasons[0]} and {reasons[1]}"

    if condition == "lost":
        message = (
            f"\"{book_name}\" was {reason_text}. "
            f"A fine of \u20b9{total_fine} has been applied."
        )
    else:
        message = (
            f"\"{book_name}\" was {reason_text}. "
            f"A fine of \u20b9{total_fine} has been applied."
        )

    title = f"Fine Issued \u2014 \u20b9{total_fine}"

    extra = {
        "title": title,
        "book_name": book_name,
        "fine_amount": total_fine,
        "days_late": days_late,
        "book_condition": condition,
        "reason": reason_text
    }

    return {
        "title": title,
        "message": message,
        "extra": extra
    }


def promote_next_waitlist_user(book_id, book_name=None):
    """
    Automatic Waitlist Promotion.

    Called right after a book copy comes back into the library
    (a normal, non-lost return). If anyone is waiting for that
    exact book, the longest-waiting ELIGIBLE user is automatically
    issued the returned copy:

        - removed from the waitlist
        - given a brand-new active borrow record (borrowed today,
          due per the normal get_borrow_days() rule)
        - the book's available copies are decremented back down,
          since the copy never actually sat "available" - it went
          straight back out to them

    If nobody is waiting, or every waiting entry is ineligible
    (malformed, references a deleted user, or that user already
    has this exact book out), nothing is created and None is
    returned - the caller should fall back to its normal
    "returned successfully" message in that case.

    This function loads/saves the waitlist, books and
    borrow records on its own, so any route can call it as
    the very last step of a return, right after that route has
    already saved its own book/record changes.
    """

    book_id = str(book_id).strip()

    waitlist = load_waitlist()
    users = load_users()
    books = load_books()
    borrow_records = load_borrow_records()

    known_emails = {
        str(u.get("email", "")).strip().lower()
        for u in users
        if isinstance(u, dict)
    }

    waitlist_changed = False
    promoted_entry = None
    index = 0

    while index < len(waitlist):

        entry = waitlist[index]

        # Malformed entry - drop it and keep looking.
        if not isinstance(entry, dict):
            waitlist.pop(index)
            waitlist_changed = True
            continue

        entry_book_id = str(entry.get("book_id", "")).strip()
        entry_email = str(
            entry.get("user_email", "")
        ).strip().lower()

        # Not for this book - leave it untouched, check the next one.
        if entry_book_id != book_id:
            index += 1
            continue

        # No usable email on this entry - drop it, it can never be
        # served, and continue looking for an eligible waiter.
        if not entry_email:
            waitlist.pop(index)
            waitlist_changed = True
            continue

        # Waitlisted user no longer exists - drop the stale entry
        # and move on to the next waiting user.
        if entry_email not in known_emails:
            waitlist.pop(index)
            waitlist_changed = True
            continue

        # User already has an active borrow of this exact book -
        # skip them (leave their entry in place) and try the next
        # waiting user instead.
        already_has_book = any(
            isinstance(r, dict)
            and str(
                r.get("user_email", "")
            ).strip().lower() == entry_email
            and str(r.get("book_id", "")).strip() == book_id
            and r.get("returned") is False
            for r in borrow_records
        )

        if already_has_book:
            index += 1
            continue

        # Eligible! Remove them from the waitlist and issue the book.
        promoted_entry = waitlist.pop(index)
        waitlist_changed = True
        break

    if waitlist_changed:
        save_waitlist(waitlist)

    if promoted_entry is None:
        return None

    promoted_email = str(
        promoted_entry.get("user_email", "")
    ).strip().lower()

    selected_book = None

    for book in books:
        if str(book.get("book_id", "")).strip() == book_id:
            selected_book = book
            break

    name = (
        book_name
        or (selected_book.get("book_name") if selected_book else None)
        or promoted_entry.get("book_name", "the book")
    )

    issue_datetime = datetime.now()

    due_datetime = issue_datetime + timedelta(days=get_borrow_days())

    new_record = {
        "user_email": promoted_email,
        "book_id": book_id,
        "book_name": (
            selected_book.get("book_name") if selected_book else name
        ),
        "author": (
            selected_book.get("author", "") if selected_book else ""
        ),
        "category": (
            selected_book.get("category", "") if selected_book else ""
        ),
        "borrowed_date": issue_datetime.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "due_date": due_datetime.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "returned": False,
        "returned_date": None
    }

    borrow_records.append(new_record)

    save_borrow_records(borrow_records)

    # The copy that just came back is going straight back out to the
    # promoted user, so it must not remain counted as available.
    if selected_book is not None:

        try:
            current_copies = int(selected_book.get("copies", 0))
        except (TypeError, ValueError):
            current_copies = 0

        selected_book["copies"] = max(current_copies - 1, 0)

        save_books(books)

    add_notification(
        promoted_email,
        f"Good news! '{name}' you were waiting for has been "
        f"automatically issued to you. Due back on "
        f"{new_record['due_date'][:10]}.",
        "waitlist_available"
    )

    return new_record


def calculate_late_days(record, return_datetime=None):
    """
    Calculate the number of days a book is late.

    For returned books:
        due_date -> returned_date

    For active overdue books:
        due_date -> current time
    """

    try:

        due_date_text = record.get("due_date")

        if not due_date_text:

            borrowed_date = datetime.strptime(
                record.get("borrowed_date"),
                "%Y-%m-%d %H:%M:%S"
            )

            due_date = (
                borrowed_date
                + timedelta(days=get_borrow_days())
            )

        else:

            due_date = datetime.strptime(
                due_date_text,
                "%Y-%m-%d %H:%M:%S"
            )

        if return_datetime is None:

            if record.get("returned") is True:

                returned_date_text = record.get(
                    "returned_date"
                )

                if returned_date_text:

                    return_datetime = datetime.strptime(
                        returned_date_text,
                        "%Y-%m-%d %H:%M:%S"
                    )

                else:
                    return_datetime = datetime.now()

            else:

                return_datetime = datetime.now()

        difference = return_datetime - due_date

        days_late = difference.days

        if difference.total_seconds() > 0 and days_late == 0:
            days_late = 1

        return max(days_late, 0)

    except (ValueError, TypeError, AttributeError):

        return 0


def calculate_fine(record):

    days_late = calculate_late_days(record)

    late_fine = (
        days_late * LATE_FINE_PER_DAY
    )

    damage_fine = 0
    lost_fine = 0

    condition = str(
        record.get(
            "book_condition",
            "good"
        )
    ).lower().strip()

    if condition == "damaged":
        damage_fine = DAMAGE_FINE

    elif condition == "lost":
        lost_fine = LOST_FINE

    total_fine = (
        late_fine
        + damage_fine
        + lost_fine
    )

    return {
        "days_late": days_late,
        "late_fine": late_fine,
        "damage_fine": damage_fine,
        "lost_fine": lost_fine,
        "total_fine": total_fine
    }


# =========================================================
# SHARED REPORT STATS
#
# Same calculation used by the /reports page. Pulled out into
# its own helper so the compact report strip shown on the
# Admin and Librarian dashboards (directly under the Welcome
# section) always matches the numbers on the full Reports
# page, without duplicating the logic.
# =========================================================

def compute_report_stats():

    books = load_books()
    total_books = len(books)

    users = load_users()
    total_users = sum(
        1 for user in users
        if user.get("role", "").lower() == "user"
    )

    borrow_records = load_borrow_records()
    total_issued = sum(
        1 for record in borrow_records
        if record.get("returned") is False
    )

    total_fine = 0

    for record in borrow_records:

        if not isinstance(record, dict):
            continue

        if record.get("fine_status") == "Paid":
            continue

        fine_data = calculate_fine(record)

        total_fine += fine_data["total_fine"]

    return {
        "total_books": total_books,
        "total_users": total_users,
        "total_issued": total_issued,
        "total_fine": total_fine
    }


# =========================================================
# LIBRARY CHATBOT (SIMPLE RAG)
#
# "Retrieval" step: read the live books / borrow_records /
# waitlist data (and the existing fine/borrow constants)
# straight from the same JSON files and constants the rest
# of the app already uses.
#
# "Generation" step: format that retrieved data into a short,
# readable answer. There is no external AI/ML model involved -
# this keeps the chatbot simple, predictable and always in
# sync with real library data.
# =========================================================

CHATBOT_QUESTIONS = [
    {"id": "q1", "question": "Is a particular book available?"},
    {"id": "q2", "question": "How long can I keep a borrowed book?"},
    {"id": "q3", "question": "How can I renew a book?"},
    {"id": "q4", "question": "What happens if I return a book late?"},
    {"id": "q5", "question": "How does the waitlist work?"},
    {"id": "q6", "question": "How can I join a waitlist for a book?"},
    {"id": "q7", "question": "How much is the overdue fine?"},
    {"id": "q8", "question": "What is the fine for a damaged book?"},
    {"id": "q9", "question": "What is the fine for a lost book?"},
]


def _chatbot_book_availability(query):

    # "Retrieval" step for this question: the person types (part of)
    # a book title and we look it up straight from the live books
    # catalog, the same data source used everywhere else in the app.

    query = (query or "").strip()

    if not query:
        return (
            "Sure! Type the title (or part of the title) of the book "
            "you're looking for and I'll check it for you."
        )

    books = load_books()
    needle = query.lower()

    matches = []

    for book in books:

        if not isinstance(book, dict) or not book.get("book_id"):
            continue

        name = str(book.get("book_name", ""))

        if needle in name.lower():
            matches.append(book)

    if not matches:
        return (
            "I couldn't find a book matching \"{0}\" in the catalog. "
            "Please check the spelling, or browse Search Books from "
            "your dashboard.".format(query)
        )

    lines = []

    for book in matches[:5]:

        try:
            copies = int(book.get("copies", 0) or 0)
        except (TypeError, ValueError):
            copies = 0

        status = (
            "{0} copy(ies) available".format(copies)
            if copies > 0
            else "not available right now - you can join the waitlist"
        )

        lines.append(
            "{0} by {1} - {2}".format(
                book.get("book_name", "Untitled"),
                book.get("author", "Unknown"),
                status
            )
        )

    reply = (
        "Here's what I found for \"{0}\":\n".format(query)
        + "\n".join("- " + line for line in lines)
    )

    if len(matches) > 5:
        reply += "\n...and {0} more matching title(s).".format(
            len(matches) - 5
        )

    return reply


def _chatbot_borrow_period():

    return (
        "You can keep a borrowed book for {0} days from the date it "
        "is issued to you. If you need more time, you can renew it "
        "for {1} extra day(s) before it's due back.".format(
            get_borrow_days(), get_renewal_days()
        )
    )


def _chatbot_how_to_renew():

    return (
        "Open Renew Books from your dashboard - a 'Request Renewal' "
        "button appears there on the single day before a book's due "
        "date. Clicking it sends a renewal request to the librarian "
        "for approval; it does not renew the book immediately. If "
        "approved, you get {0} extra day(s) before the book is due. "
        "Each book can only be renewed once per borrowing, and "
        "overdue books can no longer request renewal - return them "
        "instead.".format(get_renewal_days())
    )


def _chatbot_late_return():

    return (
        "If a book isn't returned by its due date, a late fine of "
        "\u20b9{0} is charged for every day it stays overdue, "
        "counted from the due date until the day it's actually "
        "returned. You can still return the book at any time - the "
        "fine simply keeps adding up until it's back.".format(
            LATE_FINE_PER_DAY
        )
    )


def _chatbot_join_waitlist():

    return (
        "Open Borrow Books from your dashboard and enter the Book ID "
        "of the title you want. If every copy is already issued, "
        "you're automatically added to its waitlist - there's no "
        "separate form. As soon as a copy is returned, it's issued "
        "to the longest-waiting member and they're notified right "
        "away."
    )


def _chatbot_waitlist_info():

    waitlist = load_waitlist()

    per_book = {}

    for entry in waitlist:

        if not isinstance(entry, dict):
            continue

        name = entry.get("book_name", "Unknown Book")
        per_book[name] = per_book.get(name, 0) + 1

    base = (
        "If a book you want is unavailable, you're automatically "
        "added to its waitlist when you try to borrow it. As soon "
        "as a copy of that book is returned, it is automatically "
        "issued to the longest-waiting eligible member on the "
        "waitlist, and they're notified right away."
    )

    if not per_book:
        return base + " There is currently no one on any waitlist."

    lines = [
        "{0} - {1} waiting".format(name, count)
        for name, count in sorted(
            per_book.items(), key=lambda item: item[1], reverse=True
        )[:5]
    ]

    return (
        base
        + "\n\nCurrently waiting:\n"
        + "\n".join("- " + line for line in lines)
    )


def _chatbot_overdue_fine():

    return (
        "An overdue book is fined ₹{0} for every day it is late, "
        "counted from the due date until the day it is "
        "returned.".format(LATE_FINE_PER_DAY)
    )


def _chatbot_damage_fine():

    return (
        "A book returned in damaged condition is fined a flat "
        "₹{0}, in addition to any late fine if it was also "
        "overdue.".format(DAMAGE_FINE)
    )


def _chatbot_lost_fine():

    return "A lost book is fined a flat ₹{0}.".format(LOST_FINE)


def get_chatbot_answer(question_id, book_query=None):

    # q1 needs the extra free-text book title the person typed, so
    # it is handled separately from the other, static-answer
    # questions below.
    if question_id == "q1":
        return _chatbot_book_availability(book_query)

    handlers = {
        "q2": _chatbot_borrow_period,
        "q3": _chatbot_how_to_renew,
        "q4": _chatbot_late_return,
        "q5": _chatbot_waitlist_info,
        "q6": _chatbot_join_waitlist,
        "q7": _chatbot_overdue_fine,
        "q8": _chatbot_damage_fine,
        "q9": _chatbot_lost_fine,
    }

    handler = handlers.get(question_id)

    if handler is None:
        return "Sorry, I don't have an answer for that question yet."

    try:
        return handler()
    except Exception:
        return (
            "Sorry, something went wrong while looking that up. "
            "Please try again."
        )


@app.route("/chatbot-ask", methods=["POST"])
def chatbot_ask():

    # The RAG assistant only lives on the normal User Dashboard now,
    # so only a logged-in user (not admin/librarian) may query it.
    if "email" not in session:
        return jsonify(
            {"error": "Please log in to use the library assistant."}
        ), 401

    if session.get("role") != "user":
        return jsonify(
            {"error": "The library assistant is only available on "
                       "the User Dashboard."}
        ), 403

    data = request.get_json(silent=True) or {}

    question_id = str(data.get("question_id", "")).strip()
    book_query = str(data.get("book_query", "")).strip()

    valid_ids = {question["id"] for question in CHATBOT_QUESTIONS}

    if question_id not in valid_ids:
        return jsonify(
            {"error": "Sorry, I don't recognize that question."}
        ), 400

    answer = get_chatbot_answer(question_id, book_query)

    return jsonify({"answer": answer})


# =========================================================
# ADMIN ANALYTICS DASHBOARD
#
# Separate page (own card on the Admin Dashboard) showing
# summary numbers and charts built entirely from the existing
# library data - books, users, borrow records
# and waitlist (all in PostgreSQL) - using the same helper functions and fine
# rules already used elsewhere in the app (e.g. calculate_fine,
# get_borrow_days()). No dummy/static values are used.
# =========================================================

@app.route("/admin-analytics-dashboard")
def admin_analytics_dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        flash(
            "Only admins can access the analytics dashboard.",
            "error"
        )
        return redirect(url_for("back_to_dashboard"))

    users = load_users()
    books = load_books()
    borrow_records = load_borrow_records()
    waitlist = load_waitlist()

    valid_books = [
        book for book in books
        if isinstance(book, dict) and book.get("book_id")
    ]

    valid_records = [
        record for record in borrow_records
        if isinstance(record, dict) and record.get("user_email")
    ]

    valid_waitlist = [
        entry for entry in waitlist if isinstance(entry, dict)
    ]

    # ----------------------------------------------------
    # SUMMARY NUMBERS
    # ----------------------------------------------------

    total_users = sum(
        1 for user in users
        if isinstance(user, dict)
        and str(user.get("role", "")).lower() == "user"
    )

    total_books = len(valid_books)

    total_issued = sum(
        1 for record in valid_records
        if record.get("returned") is False
    )

    total_available = 0

    for book in valid_books:
        try:
            total_available += int(book.get("copies", 0) or 0)
        except (TypeError, ValueError):
            pass

    total_borrowings = len(valid_records)

    active_waitlist = len(valid_waitlist)

    outstanding_fines = 0

    for record in valid_records:

        if record.get("fine_status") == "Paid":
            continue

        fine_data = calculate_fine(record)
        outstanding_fines += fine_data["total_fine"]

    # ----------------------------------------------------
    # BORROWINGS OVER TIME (grouped by month)
    # ----------------------------------------------------

    month_counts = {}

    for record in valid_records:

        borrowed_date = record.get("borrowed_date")

        if not borrowed_date:
            continue

        try:
            borrowed_dt = datetime.strptime(
                borrowed_date, "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, TypeError):
            continue

        month_key = borrowed_dt.strftime("%Y-%m")

        month_counts[month_key] = month_counts.get(month_key, 0) + 1

    borrowings_over_time = sorted(month_counts.items())

    # ----------------------------------------------------
    # BOOKS BY GENRE
    # ----------------------------------------------------

    genre_counts = {}

    for book in valid_books:
        genre = book.get("category") or "Uncategorized"
        genre_counts[genre] = genre_counts.get(genre, 0) + 1

    books_by_genre = sorted(
        genre_counts.items(), key=lambda item: item[1], reverse=True
    )

    # ----------------------------------------------------
    # MOST BORROWED BOOKS
    # ----------------------------------------------------

    book_counts = {}

    for record in valid_records:
        name = record.get("book_name", "Unknown Book")
        book_counts[name] = book_counts.get(name, 0) + 1

    most_borrowed_books = sorted(
        book_counts.items(), key=lambda item: item[1], reverse=True
    )[:5]

    # ----------------------------------------------------
    # MOST ACTIVE USERS
    # ----------------------------------------------------

    email_to_name = {}

    for user in users:
        if isinstance(user, dict):
            email_to_name[str(user.get("email", "")).strip().lower()] = (
                user.get("name") or user.get("email", "")
            )

    user_counts = {}

    for record in valid_records:

        email = str(record.get("user_email", "")).strip().lower()

        if not email:
            continue

        user_counts[email] = user_counts.get(email, 0) + 1

    most_active_users = [
        (email_to_name.get(email, email), count)
        for email, count in sorted(
            user_counts.items(), key=lambda item: item[1], reverse=True
        )[:5]
    ]

    # ----------------------------------------------------
    # FINE STATISTICS (paid vs outstanding, by fine type)
    # ----------------------------------------------------

    fine_paid = {"late": 0, "damage": 0, "lost": 0}
    fine_outstanding = {"late": 0, "damage": 0, "lost": 0}

    for record in valid_records:

        if record.get("fine_status") == "Paid":

            fine_paid["late"] += record.get("late_fine", 0) or 0
            fine_paid["damage"] += record.get("damage_fine", 0) or 0
            fine_paid["lost"] += record.get("lost_fine", 0) or 0

        else:

            fine_data = calculate_fine(record)

            fine_outstanding["late"] += fine_data["late_fine"]
            fine_outstanding["damage"] += fine_data["damage_fine"]
            fine_outstanding["lost"] += fine_data["lost_fine"]

    # ----------------------------------------------------
    # SPLIT INTO PLAIN LABEL/VALUE LISTS FOR THE CHARTS
    # ----------------------------------------------------

    borrowings_time_labels = [item[0] for item in borrowings_over_time]
    borrowings_time_values = [item[1] for item in borrowings_over_time]

    genre_labels = [item[0] for item in books_by_genre]
    genre_values = [item[1] for item in books_by_genre]

    top_books_labels = [item[0] for item in most_borrowed_books]
    top_books_values = [item[1] for item in most_borrowed_books]

    top_users_labels = [item[0] for item in most_active_users]
    top_users_values = [item[1] for item in most_active_users]

    return render_template(
        "analytics_dashboard.html",
        total_users=total_users,
        total_books=total_books,
        total_issued=total_issued,
        total_available=total_available,
        total_borrowings=total_borrowings,
        active_waitlist=active_waitlist,
        outstanding_fines=outstanding_fines,
        borrowings_time_labels=borrowings_time_labels,
        borrowings_time_values=borrowings_time_values,
        genre_labels=genre_labels,
        genre_values=genre_values,
        top_books_labels=top_books_labels,
        top_books_values=top_books_values,
        top_users_labels=top_users_labels,
        top_users_values=top_users_values,
        fine_paid=fine_paid,
        fine_outstanding=fine_outstanding
    )


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":
    # Local development only. On a server, run with gunicorn instead
    # (see DEPLOYMENT.md): `gunicorn app:app`. The Werkzeug debugger is
    # off unless FLASK_DEBUG=1 is set, because it must never be exposed
    # on a public deployment.
    app.run(debug=os.environ.get("FLASK_DEBUG", "").strip().lower()
            in ("1", "true", "yes", "on"))
