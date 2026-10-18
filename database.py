"""
database.py - where the server keeps its data

    DATABASE_URL set      a PostgreSQL database, normally on Neon (neon.tech): the connection string from the
                          Neon console, e.g. postgresql://user:password@ep-....neon.tech/neondb?sslmode=require
    DATABASE_URL not set  a local SQLite file (RSR_DB, default road_speed.db next to this file); used by the tests

The server writes each query once, SQLite style (? placeholders), and this module runs it on either database.
As with sqlite3, a transaction starts at the first INSERT, UPDATE or DELETE and ends at commit(); reads on their
own need no transaction, which saves round trips to Neon. Connections come from a pool that checks one left idle
before reusing it, so a connection Neon closed while its compute was suspended is replaced instead of failing.
"""
import atexit
import functools
import os
import re
import sqlite3
import threading
import time
import weakref

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Read DATABASE_URL and RSR_DB from config.env next to this file (environment variables win)."""
    path = os.path.join(BASE, "config.env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_config()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("RSR_DB", os.path.join(BASE, "road_speed.db"))
POOL_SIZE = int(os.environ.get("RSR_DB_POOL", "10"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (code TEXT PRIMARY KEY, name TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS stations (code TEXT PRIMARY KEY, name TEXT, sector TEXT, verified INTEGER, pw_hash TEXT);
CREATE TABLE IF NOT EXISTS admins (code TEXT PRIMARY KEY, name TEXT, agency TEXT, pw_hash TEXT);
CREATE TABLE IF NOT EXISTS drivers (code TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, agency TEXT,
    car_code TEXT, pw_hash TEXT, created REAL, source TEXT, sample INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS issued_credentials (code TEXT PRIMARY KEY, password TEXT, issued REAL);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, role TEXT, data TEXT, exp REAL);
CREATE TABLE IF NOT EXISTS sector_limits (sector TEXT PRIMARY KEY, limit_kmh INTEGER, set_by TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS agency_policies (sector TEXT, agency TEXT, mode TEXT, PRIMARY KEY (sector, agency));
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, sector TEXT, station_code TEXT,
    station_name TEXT, target_type TEXT, target TEXT, message TEXT, created REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, ts REAL,
    agency TEXT, driver_code TEXT, driver_name TEXT, car_code TEXT, sector TEXT, speed REAL, limit_kmh REAL,
    severity TEXT, speed_reduced INTEGER, kind TEXT, source TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS live (driver_code TEXT PRIMARY KEY, name TEXT, agency TEXT, car_code TEXT, sector TEXT,
    trip_from TEXT, trip_to TEXT, speed REAL, limit_kmh REAL, status TEXT, progress REAL, lat REAL, lon REAL,
    source TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS segments (code TEXT PRIMARY KEY, name TEXT, sector TEXT, rsu TEXT, lat REAL, lon REAL,
    radius_km REAL, limit_kmh INTEGER, set_by TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS limit_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, segment TEXT,
    old_limit INTEGER, new_limit INTEGER, set_by TEXT);
CREATE TABLE IF NOT EXISTS detections (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, rsu TEXT, segment TEXT,
    sector TEXT, driver_code TEXT, agency TEXT, car_code TEXT, speed REAL, lat REAL, lon REAL, distance_km REAL);
CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, driver_code TEXT, agency TEXT,
    car_code TEXT, segment TEXT, sector TEXT, speed REAL, limit_kmh REAL, status TEXT, lat REAL, lon REAL, source TEXT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, sector TEXT,
    segment TEXT, driver_code TEXT, driver_name TEXT, agency TEXT, car_code TEXT, speed REAL, limit_kmh REAL,
    message TEXT, event_id TEXT, ack_by TEXT, ack_ts REAL);
"""
# Columns added after the first release; older databases are upgraded in place.
MIGRATIONS = {
    "stations": ["email TEXT"],
    "admins": ["email TEXT"],
    "events": ["segment TEXT", "lat REAL", "lon REAL", "received REAL"],
    "live": ["segment TEXT", "in_coverage INTEGER", "over_since REAL", "logged REAL"],
}
INDEXES = """
CREATE INDEX IF NOT EXISTS events_segment_ts ON events (segment, ts);
CREATE INDEX IF NOT EXISTS telemetry_segment_ts ON telemetry (segment, ts);
CREATE INDEX IF NOT EXISTS detections_segment_ts ON detections (segment, ts);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);
"""


def connect():
    """A connection with the sqlite3 interface (execute, executemany, executescript, commit, close)."""
    if POSTGRES:
        return PostgresConnection(_get_pool())
    conn = sqlite3.connect(SQLITE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def describe():
    """The database in use, for the start-up message (never shows the password)."""
    if not POSTGRES:
        return f"SQLite {SQLITE_PATH}"
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(DATABASE_URL)
    host = info.get("host", "?")
    return f"{'Neon' if host.endswith('neon.tech') else 'PostgreSQL'} {host}/{info.get('dbname', '')}"


def columns(conn, table):
    """Names of the columns of a table (empty when the table does not exist)."""
    if POSTGRES:
        return {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns"
                                           " WHERE table_schema=current_schema() AND table_name=?", (table,))}
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def create_schema(conn):
    """Create the tables and add the newer columns to an older database. Returns True for a new database."""
    new = not columns(conn, "admins")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = columns(conn, table)
        for col in cols:
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    conn.executescript(INDEXES)
    conn.commit()
    return new


def upsert(table, cols, key):
    """INSERT that replaces the row with the same key (SQLite's INSERT OR REPLACE, in SQL both databases run)."""
    names = [c.strip() for c in cols.split(",")]
    keys = {k.strip() for k in key.split(",")}
    return (f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
            f" ON CONFLICT ({key}) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in names if c not in keys)}")


def floor_int(expr):
    """SQL for the whole part of a positive number (CAST rounds in PostgreSQL but truncates in SQLite)."""
    return f"CAST(FLOOR({expr}) AS BIGINT)" if POSTGRES else f"CAST({expr} AS INTEGER)"


# ---------------------------------------------------------------- PostgreSQL (Neon)
CHECK_AFTER_IDLE = 5      # seconds: a connection unused for longer is tested before it is handed out
_pool = None
_pool_lock = threading.Lock()
_returned = weakref.WeakKeyDictionary()   # connection -> when it last went back to the pool
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE)\b", re.I)


def _check(conn):
    if time.monotonic() - _returned.get(conn, 0) > CHECK_AFTER_IDLE:
        from psycopg_pool import ConnectionPool
        ConnectionPool.check_connection(conn)  # raises when the connection is dead; the pool then opens a new one


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            import psycopg
            from psycopg_pool import ConnectionPool
            psycopg.connect(DATABASE_URL, connect_timeout=30).close()  # a wrong DATABASE_URL stops here, saying why
            # prepare_threshold=None: no server-side prepared statements, which Neon's pooled endpoint may not keep
            _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=POOL_SIZE, open=True, name="rsr", check=_check,
                                   kwargs={"autocommit": True, "row_factory": _row_factory, "prepare_threshold": None})
            atexit.register(_pool.close)
            _pool.wait(timeout=60)
    return _pool


@functools.lru_cache(maxsize=1024)
def _pg_sql(sql):
    """SQLite-style SQL to PostgreSQL: %s placeholders, and identity / double precision columns in DDL."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    if sql.lstrip().upper().startswith(("CREATE", "ALTER")):
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
        sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)  # PostgreSQL REAL is single precision: too coarse for times
    return sql


class Row:
    """A result row read by column name or position, like sqlite3.Row; dict(row) gives {column: value}."""
    __slots__ = ("_values", "_index")

    def __init__(self, values, index):
        self._values, self._index = values, index

    def __getitem__(self, key):
        return self._values[key if isinstance(key, int) else self._index[key]]

    def keys(self):
        return list(self._index)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _row_factory(cursor):
    index = {c.name: i for i, c in enumerate(cursor.description or ())}
    return lambda values: Row(values, index)


class PostgresConnection:
    """A pooled PostgreSQL connection with the parts of the sqlite3.Connection interface the server uses."""

    def __init__(self, pool):
        self._pool, self._conn = pool, pool.getconn()

    def _begin_if_write(self, sql):
        if _WRITE.match(sql) and self._conn.info.transaction_status.name == "IDLE":
            self._conn.execute("BEGIN")

    def execute(self, sql, params=()):
        self._begin_if_write(sql)
        return self._conn.execute(_pg_sql(sql), tuple(params))

    def executemany(self, sql, seq):
        self._begin_if_write(sql)
        cur = self._conn.cursor()
        cur.executemany(_pg_sql(sql), [tuple(p) for p in seq])
        return cur

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """Give the connection back to the pool. Changes not committed are discarded, as with sqlite3."""
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        try:
            if conn.info.transaction_status.name != "IDLE":
                conn.rollback()
        except Exception:  # noqa: BLE001  a broken connection: the pool replaces it
            pass
        _returned[conn] = time.monotonic()
        self._pool.putconn(conn)
