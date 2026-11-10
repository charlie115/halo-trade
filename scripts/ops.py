#!/usr/bin/env python3
"""Operator tools for the database at HALO_DB_PATH (stdlib only).

    HALO_DB_PATH=/data/halo.sqlite scripts/ops.py pause|resume|status
    HALO_DB_PATH=/data/halo.sqlite scripts/ops.py reset-risk USER_ID

`pause` blocks new entries for every user, `resume` lifts that. Closing is never blocked.
`reset-risk` sets a user's day-start and peak to their last equity snapshot, for a user stuck at
a daily-loss or drawdown limit after a transfer the watcher could not classify.
The running server reads the switch on every check, so no restart is needed. The database must
already exist: a mistyped path must fail, not "pause" a new empty file.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

KEY = "kill_switch"
ON_VALUES = ("1", "true", "on")  # what Service.kill_switch() treats as on


def open_db(path: str) -> sqlite3.Connection:
    # as_uri() percent-encodes the path and mode=rw refuses to create a missing file.
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=30)


def read_switch(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (KEY,)).fetchone()
    return row is not None and row[0] in ON_VALUES


def write_switch(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute(
        "INSERT INTO system_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, "1" if on else "0"),
    )


def reset_risk(conn: sqlite3.Connection, user_id: int) -> str | None:
    """The last equity snapshot becomes day-start and peak. Returns it, or None if there is none."""
    row = conn.execute(
        "SELECT equity FROM equity_snapshots WHERE user_id=? ORDER BY ts_ms DESC LIMIT 1", (user_id,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE user_risk_state SET day_start_equity=?, peak_equity=? WHERE user_id=?", (row[0], row[0], user_id)
    )
    return row[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="halo-trade operator tools")
    parser.add_argument("command", choices=("pause", "resume", "status", "reset-risk"))
    parser.add_argument("user_id", type=int, nargs="?")
    args = parser.parse_args()
    command = args.command
    if command == "reset-risk" and args.user_id is None:
        parser.error("reset-risk needs a USER_ID")
    path = os.environ.get("HALO_DB_PATH")
    if not path:
        print("HALO_DB_PATH is not set", file=sys.stderr)
        return 2
    try:
        conn = open_db(path)
        if command == "reset-risk":
            equity = reset_risk(conn, args.user_id)
            if equity is None:
                print(f"no equity snapshot for user {args.user_id}", file=sys.stderr)
                return 1
            print(f"risk_reset user_id={args.user_id} equity={equity}")
            return 0
        if command != "status":
            write_switch(conn, command == "pause")
        on = read_switch(conn)
    except sqlite3.Error as exc:
        print(f"cannot use database at {path}: {exc}", file=sys.stderr)
        return 1
    print(f"kill_switch={'on' if on else 'off'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
