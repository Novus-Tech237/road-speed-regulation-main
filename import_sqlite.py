"""
import_sqlite.py - copy a local SQLite database (road_speed.db) into the Neon database named by DATABASE_URL

    python import_sqlite.py             copies road_speed.db (or the file set in RSR_DB)
    python import_sqlite.py other.db    copies that file

Run it once, before the server first starts on Neon, so accounts, passwords and history carry over. It copies
only into an empty database (otherwise it stops and changes nothing) and only reads the SQLite file.
"""
import os
import sqlite3
import sys

import database


def main():
    if not database.POSTGRES:
        sys.exit("Set DATABASE_URL in config.env to the Neon connection string first.")
    path = sys.argv[1] if len(sys.argv) > 1 else database.SQLITE_PATH
    if not os.path.exists(path):
        sys.exit(f"{path} not found.")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = database.connect()
    database.create_schema(dst)
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    used = [t for t in tables if database.columns(dst, t) and dst.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
    if used:
        sys.exit(f"{database.describe()} already has data (tables: {', '.join(used)}). Nothing was copied.")
    print(f"Copying {path} into {database.describe()}")
    for table in tables:
        target = database.columns(dst, table)
        if not target:
            print(f"  {table:<20} skipped (not used by this version)")
            continue
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})") if r[1] in target]
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if rows:
            dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        if "id" in cols:  # new rows continue after the copied ids
            dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 0) + 1, false)"
                        f" FROM {table}")
        print(f"  {table:<20} {len(rows)} rows")
    dst.commit()
    dst.close()
    print("Done. Start the server: it now uses Neon.")


if __name__ == "__main__":
    main()
