"""
db.py - PostgreSQL access layer for the Library Management System.

WHY THIS FILE EXISTS
--------------------
app.py used to talk to a local SQLite file (library.db). It now talks to ONE
central, hosted PostgreSQL database (DATABASE_URL), so the Admin, Librarians
and Users of the deployed site all read and write the same live data.

app.py's existing code was written against the sqlite3 API (conn.execute(...),
"?" placeholders, row["col"] AND row[0], cur.lastrowid, cur.rowcount). This
module provides that same small API on top of PostgreSQL (psycopg 3 + a
connection pool), so the hundreds of existing queries keep working unchanged.

It also replaces the old "DELETE the whole table, then re-insert every row"
save pattern (see sync_table below). That pattern was tolerable for one local
file, but on a shared live database it would let two people working at the
same moment silently overwrite each other, and PostgreSQL would reject it once
renewal_requests rows reference borrow_records.
"""

import os
import re
import threading
from collections import Counter

import psycopg
from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool


class DatabaseConfigError(RuntimeError):
    """DATABASE_URL is missing/unusable."""


class ConcurrentUpdateError(Exception):
    """
    Someone else changed (or deleted/created) the same row between the moment
    this request read it and the moment it tried to save. Nothing is silently
    overwritten; the request is refused and the user is asked to retry.
    """


# ---------------------------------------------------------------------------
# Table definitions used by the row-level save (sync_table). Column order here
# is the order of values in every tuple passed to / from sync_table.
# ---------------------------------------------------------------------------

TABLES = {
    "users": {
        "cols": ["email", "name", "password", "role"],
        "key": ["email"],
        "auto": None,
    },
    "books": {
        "cols": ["book_id", "book_name", "author", "publisher", "isbn",
                 "category", "copies"],
        "key": ["book_id"],
        "auto": None,
    },
    "borrow_records": {
        "cols": [
            "id", "user_email", "book_id", "book_name", "author", "category",
            "borrowed_date", "due_date", "returned", "returned_date",
            "return_requested", "return_requested_date", "book_condition",
            "days_late", "late_fine", "damage_fine", "lost_fine",
            "total_fine", "fine_status", "fine_paid_date",
            "renewal_count", "last_renewed_at",
        ],
        "key": ["id"],
        "auto": "id",
    },
    "notifications": {
        "cols": ["id", "user_email", "message", "type", "created_date",
                 "read", "title", "book_name", "fine_amount", "days_late",
                 "book_condition", "reason"],
        "key": ["id"],
        "auto": "id",
    },
    # The waitlist has no natural key exposed to the app (rows are plain
    # dicts without an id), so it is synchronised as a multiset of rows.
    "waitlist": {
        "cols": ["user_email", "user_name", "book_id", "book_name",
                 "requested_date"],
        "key": None,
        "auto": "id",
    },
}


def q(name):
    """Quote an identifier (so e.g. the column "read" can never clash)."""
    return '"' + name.replace('"', '""') + '"'


def qcols(cols):
    return ", ".join(q(c) for c in cols)


# ---------------------------------------------------------------------------
# Rows that behave like sqlite3.Row: row["col"], row[0], dict(row), keys()
# ---------------------------------------------------------------------------

class Row(tuple):
    def __new__(cls, values, cols):
        obj = super().__new__(cls, values)
        obj._cols = cols
        return obj

    def __getitem__(self, item):
        if isinstance(item, str):
            try:
                return tuple.__getitem__(self, self._cols.index(item))
            except ValueError:
                raise IndexError("No item with that key: %r" % item)
        return tuple.__getitem__(self, item)

    def keys(self):
        return list(self._cols)


def _row_factory(cursor):
    cols = [d.name for d in cursor.description] if cursor.description else []

    def make(values):
        return Row(values, cols)

    return make


# ---------------------------------------------------------------------------
# SQL translation: the app's SQL uses sqlite-style "?" placeholders.
# ---------------------------------------------------------------------------

def translate_sql(sql, has_params):
    out = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        if ch == "%" and has_params:
            out.append("%")
        i += 1
    return "".join(out)


# tables whose INSERTs the app expects cur.lastrowid for
_AUTO_PK = {
    "password_reset_otps": "id",
    "renewal_requests": "renewal_request_id",
    "borrow_records": "id",
    "waitlist": "id",
    "notifications": "id",
    "system_settings": "setting_id",
}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+\"?(\w+)\"?", re.IGNORECASE)


class Cursor:
    def __init__(self, raw):
        self._c = raw
        self.lastrowid = None

    def execute(self, sql, params=None):
        if params is not None and len(params) == 0:
            params = None
        has_params = params is not None
        text = translate_sql(sql, has_params)
        want_id = False
        m = _INSERT_RE.match(sql)
        if m and "returning" not in sql.lower():
            pk = _AUTO_PK.get(m.group(1).lower())
            if pk:
                text = text.rstrip().rstrip(";") + " RETURNING " + pk
                want_id = True
        self._c.execute(text, params)
        if want_id:
            row = self._c.fetchone()
            self.lastrowid = row[0] if row else None
        return self

    def executemany(self, sql, seq):
        seq = list(seq)
        self._c.executemany(translate_sql(sql, True), seq)
        return self

    def fetchone(self):
        try:
            return self._c.fetchone()
        except psycopg.ProgrammingError:
            return None

    def fetchall(self):
        try:
            return self._c.fetchall()
        except psycopg.ProgrammingError:
            return []

    def fetchmany(self, size=1):
        try:
            return self._c.fetchmany(size)
        except psycopg.ProgrammingError:
            return []

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def description(self):
        return self._c.description

    def close(self):
        self._c.close()

    def __iter__(self):
        return iter(self.fetchall())


# ---------------------------------------------------------------------------
# Pool + connection wrapper
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()
_tls = threading.local()


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseConfigError(
            "DATABASE_URL is not set. Put your PostgreSQL connection string "
            "in the DATABASE_URL environment variable (or in the .env file). "
            "See DEPLOYMENT.md."
        )
    return url


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                pool = ConnectionPool(
                    conninfo=_database_url(),
                    min_size=_env_int("DB_POOL_MIN", 1),
                    max_size=_env_int("DB_POOL_MAX", 5),
                    timeout=_env_int("DB_POOL_TIMEOUT", 30),
                    max_idle=300,
                    max_lifetime=1800,
                    check=ConnectionPool.check_connection,
                    kwargs={
                        "row_factory": _row_factory,
                        # safe with PgBouncer/pooled endpoints
                        "prepare_threshold": None,
                        "connect_timeout": 20,
                    },
                    open=False,
                )
                pool.open(wait=True, timeout=60)
                _pool = pool
    return _pool


def _open_connections():
    s = getattr(_tls, "open", None)
    if s is None:
        s = _tls.open = set()
    return s


def _snapshots():
    d = getattr(_tls, "snaps", None)
    if d is None:
        d = _tls.snaps = {}
    return d


class Connection:
    def __init__(self):
        self._pool = get_pool()
        self._raw = self._pool.getconn()
        _open_connections().add(self)

    def cursor(self):
        return Cursor(self._raw.cursor())

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        raw, self._raw = self._raw, None
        if raw is None:
            return
        _open_connections().discard(self)
        try:
            if not raw.closed and \
                    raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                raw.rollback()
        except Exception:
            pass
        self._pool.putconn(raw)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def get_connection():
    return Connection()


def end_request():
    """
    Called at the end of every web request (and at the start of the next):
    returns any connection a code path forgot to close to the pool, and
    forgets this request's remembered row snapshots.
    """
    for conn in list(_open_connections()):
        conn.close()
    _snapshots().clear()


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Row-level saving (replaces "DELETE everything, re-insert everything")
#
# load_*() in app.py calls remember_snapshot() with the rows exactly as they
# were read. save_*() calls sync_table() with the full list the route wants
# the table to contain. sync_table compares that list to what THIS request
# originally read and writes ONLY the differences:
#   * rows that were removed from the list      -> DELETE
#   * rows that changed                         -> UPDATE ... WHERE row is still
#                                                  exactly what we read
#   * rows that are new                         -> INSERT
# Rows nobody touched are never rewritten, so two people editing DIFFERENT
# rows at the same time no longer clobber each other. If two people change the
# SAME row at the same time, the second one gets ConcurrentUpdateError instead
# of silently erasing the first one's change (e.g. book copy counts).
# ---------------------------------------------------------------------------

def _select_cols(table):
    spec = TABLES[table]
    return (["id"] if spec["key"] is None else []) + spec["cols"]


def _build_snapshot(table, rows):
    spec = TABLES[table]
    if spec["key"] is None:
        return [(r[0], tuple(r[1:])) for r in rows]          # [(id, values)]
    kidx = [spec["cols"].index(k) for k in spec["key"]]
    return {tuple(r[i] for i in kidx): tuple(r) for r in rows}


def remember_snapshot(table, rows):
    _snapshots()[table] = _build_snapshot(table, [tuple(r) for r in rows])


def _read_current(conn, table):
    spec = TABLES[table]
    order = "id" if spec["key"] is None else ", ".join(q(k) for k in spec["key"])
    cur = conn.execute(
        "SELECT " + qcols(_select_cols(table)) + " FROM " + q(table) +
        " ORDER BY " + order
    )
    return _build_snapshot(table, [tuple(r) for r in cur.fetchall()])


def _cas_clause(cols):
    return " AND ".join(q(c) + " IS NOT DISTINCT FROM ?" for c in cols)


def sync_table(table, new_rows):
    """
    new_rows: list of value-tuples in TABLES[table]["cols"] order, already
    converted to database values. Returns {input_index: new_auto_id} for rows
    that were inserted and got a database-assigned id.
    """
    spec = TABLES[table]
    new_rows = [tuple(r) for r in new_rows]
    conn = get_connection()
    try:
        base = _snapshots().get(table)
        if base is None:
            base = _read_current(conn, table)
        if spec["key"] is None:
            assigned, new_base = _sync_multiset(conn, table, base, new_rows)
        else:
            assigned, new_base = _sync_keyed(conn, table, base, new_rows)
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        raise ConcurrentUpdateError(table)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _snapshots()[table] = new_base
    return assigned


def _sync_keyed(conn, table, base, new_rows):
    spec = TABLES[table]
    cols = spec["cols"]
    kidx = [cols.index(k) for k in spec["key"]]
    auto = spec["auto"]
    auto_idx = cols.index(auto) if auto else None

    keyed = {}
    fresh = []                                   # (input_index, tuple) w/o id
    for i, t in enumerate(new_rows):
        key = tuple(t[j] for j in kidx)
        if auto and any(k is None for k in key):
            fresh.append((i, t))
            continue
        if key in keyed:
            raise ValueError("Duplicate %s key: %r" % (table, key))
        keyed[key] = t

    deleted = [k for k in base if k not in keyed]
    updated = [(k, t) for k, t in keyed.items() if k in base and base[k] != t]
    added = [(k, t) for k, t in keyed.items() if k not in base]

    for k in deleted:
        old = base[k]
        cur = conn.execute(
            "DELETE FROM " + q(table) + " WHERE " + _cas_clause(cols),
            old
        )
        if cur.rowcount == 0:
            still = conn.execute(
                "SELECT 1 FROM " + q(table) + " WHERE " +
                _cas_clause(spec["key"]), k
            ).fetchone()
            if still is not None:
                raise ConcurrentUpdateError(table)    # changed under us

    for k, t in updated:
        old = base[k]
        set_idx = [i for i in range(len(cols)) if i not in kidx]
        cur = conn.execute(
            "UPDATE " + q(table) + " SET " +
            ", ".join(q(cols[i]) + " = ?" for i in set_idx) +
            " WHERE " + _cas_clause(cols),
            tuple(t[i] for i in set_idx) + tuple(old)
        )
        if cur.rowcount == 0:
            raise ConcurrentUpdateError(table)

    for k, t in added:
        conn.execute(
            "INSERT INTO " + q(table) + " (" + qcols(cols) + ") VALUES (" +
            ", ".join(["?"] * len(cols)) + ")", t
        )

    assigned = {}
    ins_cols = [c for c in cols if c != auto]
    for i, t in fresh:
        vals = tuple(v for c, v in zip(cols, t) if c != auto)
        cur = conn.execute(
            "INSERT INTO " + q(table) + " (" + qcols(ins_cols) + ") VALUES (" +
            ", ".join(["?"] * len(ins_cols)) + ") RETURNING " + q(auto), vals
        )
        new_id = cur.fetchone()[0]
        assigned[i] = new_id
        lst = list(t)
        lst[auto_idx] = new_id
        new_rows[i] = tuple(lst)

    new_base = {}
    for t in new_rows:
        new_base[tuple(t[j] for j in kidx)] = t
    return assigned, new_base


def _sync_multiset(conn, table, base, new_rows):
    spec = TABLES[table]
    cols = spec["cols"]
    need = Counter(new_rows)
    kept, to_delete = [], []
    for rid, vals in base:
        if need[vals] > 0:
            need[vals] -= 1
            kept.append((rid, vals))
        else:
            to_delete.append((rid, vals))

    for rid, vals in to_delete:
        conn.execute(
            "DELETE FROM " + q(table) + " WHERE id = ? AND " +
            _cas_clause(cols), (rid,) + tuple(vals)
        )          # already gone is fine

    assigned = {}
    added = []
    for i, t in enumerate(new_rows):
        if need[t] > 0:
            need[t] -= 1
            cur = conn.execute(
                "INSERT INTO " + q(table) + " (" + qcols(cols) + ") VALUES (" +
                ", ".join(["?"] * len(cols)) + ") RETURNING id", t
            )
            rid = cur.fetchone()[0]
            assigned[i] = rid
            added.append((rid, t))
    return assigned, sorted(kept + added)
