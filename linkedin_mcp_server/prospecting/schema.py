"""SQLite tables shared by the daily caps and the campaign store.

One file, created wherever the caps already write. The lead key is global on
``(lead_vanity, kind)``: the same person in a second campaign, or the same
person under a different Sales Navigator URN, is the same row.
"""

from __future__ import annotations

import sqlite3

# Separate statements. ``executescript`` commits, and a connection in
# autocommit mode has nothing to commit the first time it is used.
_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS writes (
        token TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        kind TEXT NOT NULL,
        outcome TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        filters_json TEXT,
        created_at TEXT NOT NULL,
        paused_until TEXT,
        pause_reason TEXT
    )
    """,
    # One row for the account. A refusal pauses every campaign, including one
    # created while the pause still holds. The columns on ``campaigns`` are
    # not that pause: writing one row would leave the next campaign running.
    """
    CREATE TABLE IF NOT EXISTS account_pause (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused_until TEXT NOT NULL,
        pause_reason TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leads (
        lead_vanity TEXT NOT NULL,
        kind TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        lead_urn TEXT,
        profile_url TEXT NOT NULL,
        name TEXT NOT NULL,
        title TEXT,
        company TEXT,
        state TEXT NOT NULL,
        note TEXT,
        message_text TEXT,
        navigation_token TEXT,
        write_token TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (lead_vanity, kind),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        token TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        lead_vanity TEXT NOT NULL,
        lead_kind TEXT NOT NULL,
        action TEXT NOT NULL,
        item_hash TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        consumed_at TEXT,
        PRIMARY KEY (token, lead_vanity, action),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
        FOREIGN KEY (lead_vanity, lead_kind) REFERENCES leads(lead_vanity, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL,
        lead_vanity TEXT,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        at TEXT NOT NULL,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
)


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create every prospecting table, including on a file that only had ``writes``."""
    for statement in _STATEMENTS:
        db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(writes)")}
    if "outcome" not in columns:
        db.execute("ALTER TABLE writes ADD COLUMN outcome TEXT")
