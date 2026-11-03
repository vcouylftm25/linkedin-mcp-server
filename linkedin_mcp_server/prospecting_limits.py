"""Persistent, local daily caps for prospecting writes and reads."""

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode

# Sales Navigator page loads, counted apart from invites and messages.
# Profile visits notify the person, so a read is still an action. 0 is
# unlimited, same as the other caps. The kind string is the one
# `SALES_NAV_NAVIGATION_KIND` charges; a test holds the two together.
DEFAULT_SALES_NAV_DAILY_NAVIGATIONS = 40


class ProspectingLimits:
    def __init__(
        self,
        invite_limit: int = 20,
        message_limit: int = 40,
        state_dir: str | None = None,
        *,
        navigation_limit: int = DEFAULT_SALES_NAV_DAILY_NAVIGATIONS,
    ) -> None:
        root = (
            Path(state_dir).expanduser()
            if state_dir
            else Path(
                os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
            )
            / "linkedin-mcp-server"
        )
        self.path = root / "prospecting.sqlite3"
        self.limits = {
            "invite": invite_limit,
            "message": message_limit,
            "sales_nav": navigation_limit,
        }

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute(
            "CREATE TABLE IF NOT EXISTS writes (token TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL)"
        )
        return db

    def reserve(self, kind: str) -> str:
        """Reserve a slot before browser acquisition; unresolved slots remain counted."""
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        token = uuid.uuid4().hex
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT count(*) FROM writes WHERE day=? AND kind=?", (day, kind)
            ).fetchone()[0]
            limit = self.limits[kind]
            if limit and count >= limit:
                reset = datetime.combine(
                    now.date() + timedelta(days=1), datetime.min.time()
                ).astimezone()
                seconds = max(1, int((reset - now).total_seconds()) + 1)
                raise CodedToolError(
                    f"Daily {kind} limit reached ({limit}); resets at {reset.isoformat()}. retry_safe=true",
                    ErrorCode.RATE_LIMITED,
                    retry_after=seconds,
                    structured_content={
                        "retry_safe": True,
                        "reset_at": reset.isoformat(),
                    },
                )
            db.execute(
                "INSERT INTO writes(token, day, kind) VALUES (?, ?, ?)",
                (token, day, kind),
            )
        return token

    def release(self, token: str) -> None:
        """Release a slot only when the tool knows no submission happened."""
        with closing(self._open()) as db, db:
            db.execute("DELETE FROM writes WHERE token=?", (token,))

    def report(self) -> dict[str, object]:
        """Today's used and remaining writes per kind, read-only.

        A limit of 0 means unlimited, so its remaining count is None.
        """
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        reset = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time()
        ).astimezone()
        used = {kind: 0 for kind in self.limits}
        if self.path.exists():
            with closing(sqlite3.connect(self.path, timeout=30)) as db:
                try:
                    rows = db.execute(
                        "SELECT kind, count(*) FROM writes WHERE day=? GROUP BY kind",
                        (day,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            for kind, count in rows:
                if kind in used:
                    used[kind] = count
        counts = {}
        for kind, limit in self.limits.items():
            counts[kind] = {
                "used": used[kind],
                "limit": limit,
                "remaining": max(0, limit - used[kind]) if limit else None,
            }
        return {
            "day": day,
            "resets_at": reset.isoformat(),
            "invites": counts["invite"],
            "messages": counts["message"],
            "sales_nav": counts["sales_nav"],
            "state_file": str(self.path),
        }
