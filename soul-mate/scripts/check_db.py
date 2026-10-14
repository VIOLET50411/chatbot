#!/usr/bin/env python3
"""Read-only health check for every Soul Mate SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _notify(title: str, detail: str) -> None:
    webhook = os.getenv("OPS_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        request = urllib.request.Request(
            webhook,
            data=json.dumps({"title": title, "text": detail}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10)
    except Exception as exc:
        print(f"WEBHOOK_ERROR {type(exc).__name__}")


def _check_database(db_path: Path) -> tuple[bool, str, int]:
    connection = _connect(db_path)
    try:
        quick = connection.execute("PRAGMA quick_check(20)").fetchall()
        integrity = connection.execute("PRAGMA integrity_check(20)").fetchall()
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        table_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    ok = (
        all(str(row[0]) == "ok" for row in quick)
        and all(str(row[0]) == "ok" for row in integrity)
    )
    return ok, journal_mode, table_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", "./data"),
        help="Soul Mate data directory; defaults to DATA_DIR or ./data",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    database_paths = sorted(path for path in data_dir.rglob("*.db") if path.is_file())
    if not database_paths:
        print(f"DB_NOT_FOUND {data_dir}")
        return 2

    overall_ok = True
    for db_path in database_paths:
        relative = db_path.relative_to(data_dir).as_posix()
        try:
            ok, journal_mode, table_count = _check_database(db_path)
        except sqlite3.DatabaseError as exc:
            print(f"DB_ERROR {relative}: {exc}")
            overall_ok = False
            continue
        overall_ok = overall_ok and ok
        print(
            f"DB_OK={ok} PATH={relative} JOURNAL_MODE={journal_mode} "
            f"TABLES={table_count}"
        )

    print(f"DATABASE_COUNT={len(database_paths)}")
    if not overall_ok:
        _notify("Soul Mate database health check failed", str(data_dir))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
