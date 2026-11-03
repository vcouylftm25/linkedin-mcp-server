"""Persistent, local caps for prospecting writes.

Daily caps reset at local midnight. Weekly caps cover the local Monday-to-Monday
week. ``outcome_unknown`` and ``send_failed`` stay counted: releasing them would
hand the slot back for an invitation that may already have gone out. A new day
does not delete those rows, so the week still sees them.

Profile visits are a third kind, ``navigation``. They are not invitations, and
they have their own daily budget because opening a profile is visible to the
person.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.prospecting.schema import ensure_schema

if TYPE_CHECKING:
    from linkedin_mcp_server.prospecting.repository import ProspectingRepository

DEFAULT_WEEKLY_INVITE_LIMIT = 100
DEFAULT_DAILY_NAVIGATION_LIMIT = 100

_KEPT_OUTCOMES = frozenset({"outcome_unknown", "send_failed", "sent"})


def week_bounds(now: datetime) -> tuple[str, str]:
    """Local Monday and the next Monday, as ISO dates. The end is exclusive."""
    start = now.date() - timedelta(days=now.weekday())
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


# Sales Navigator page loads are a separate budget from profile visits.
# The kind string is the one `SALES_NAV_NAVIGATION_KIND` charges.
DEFAULT_SALES_NAV_DAILY_NAVIGATIONS = 40


class ProspectingLimits:
    def __init__(
        self,
        invite_limit: int = 20,
        message_limit: int = 40,
        state_dir: str | None = None,
        *,
        weekly_invite_limit: int = DEFAULT_WEEKLY_INVITE_LIMIT,
        weekly_message_limit: int = 0,
        navigation_limit: int = DEFAULT_DAILY_NAVIGATION_LIMIT,
        sales_nav_limit: int = DEFAULT_SALES_NAV_DAILY_NAVIGATIONS,
        db_path: str | None = None,
    ) -> None:
        root = (
            Path(state_dir).expanduser()
            if state_dir
            else Path(
                os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
            )
            / "linkedin-mcp-server"
        )
        self.path = (
            Path(db_path).expanduser().resolve()
            if db_path
            else root / "prospecting.sqlite3"
        )
        self.limits = {
            "invite": invite_limit,
            "message": message_limit,
            "navigation": navigation_limit,
            "sales_nav": sales_nav_limit,
        }
        self.weekly_limits = {
            "invite": weekly_invite_limit,
            "message": weekly_message_limit,
            "navigation": 0,
            "sales_nav": 0,
        }

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        ensure_schema(db)
        return db

    def reserve(self, kind: str) -> str:
        """Reserve a slot before browser acquisition; unresolved slots stay counted."""
        now = datetime.now().astimezone()
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self.insert_reservation(db, kind, now)

    def insert_reservation(
        self, db: sqlite3.Connection, kind: str, now: datetime
    ) -> str:
        """Insert one counted write inside the caller's transaction.

        Raises :class:`CodedToolError` when a daily or weekly cap is already
        full. The caller rolls the transaction back, so a refused attempt
        leaves no row and does not mark a lead unconfirmed.
        """
        if kind not in self.limits:
            raise CodedToolError(
                f"Unknown prospecting action {kind!r}.",
                ErrorCode.INVALID_ARGUMENT,
            )
        day = now.date().isoformat()
        daily_count = db.execute(
            "SELECT count(*) FROM writes WHERE day=? AND kind=?", (day, kind)
        ).fetchone()[0]
        daily_count += self._unconfirmed_outside(db, kind, "day<>?", (day,))
        limit = self.limits[kind]
        if limit and daily_count >= limit:
            raise self._daily_error(kind, limit, now)
        start, end = week_bounds(now)
        weekly_count = db.execute(
            "SELECT count(*) FROM writes WHERE kind=? AND day>=? AND day<?",
            (kind, start, end),
        ).fetchone()[0]
        weekly_count += self._unconfirmed_outside(
            db, kind, "(day<? OR day>=?)", (start, end)
        )
        weekly = self.weekly_limits[kind]
        if weekly and weekly_count >= weekly:
            raise self._weekly_error(kind, weekly, now)
        token = uuid.uuid4().hex
        db.execute(
            "INSERT INTO writes(token, day, kind) VALUES (?, ?, ?)",
            (token, day, kind),
        )
        return token

    @staticmethod
    def _unconfirmed_outside(
        db: sqlite3.Connection,
        kind: str,
        date_predicate: str,
        dates: tuple[str, ...],
    ) -> int:
        state = f"{kind}_unconfirmed"
        return db.execute(
            f"""
            SELECT count(*) FROM writes
            JOIN leads ON leads.write_token = writes.token
            WHERE writes.kind=? AND leads.state=? AND {date_predicate}
            """,
            (kind, state, *dates),
        ).fetchone()[0]

    def release(self, token: str) -> None:
        """Release a slot only when the tool knows no submission happened."""
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.release_in(db, token)

    def release_in(self, db: sqlite3.Connection, token: str) -> None:
        # An unresolved or submitted outcome is part of the safety budget.
        # Releasing it would let a retry spend the same slot twice.
        db.execute(
            """
            DELETE FROM writes
            WHERE token=? AND (outcome IS NULL OR outcome NOT IN (?, ?, ?))
            """,
            (token, *_KEPT_OUTCOMES),
        )

    def note_outcome(self, token: str, outcome: str) -> None:
        """Record why a slot stays counted. This never deletes the row."""
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(
                "SELECT token FROM writes WHERE token=?", (token,)
            ).fetchone()
            if found is None:
                raise CodedToolError(
                    "Unknown prospecting reservation.",
                    ErrorCode.INVALID_ARGUMENT,
                )
            self.note_outcome_in(db, token, outcome)

    def note_outcome_in(self, db: sqlite3.Connection, token: str, outcome: str) -> None:
        if outcome not in _KEPT_OUTCOMES:
            raise CodedToolError(
                f"Unknown prospecting outcome {outcome!r}.",
                ErrorCode.INVALID_ARGUMENT,
            )
        db.execute("UPDATE writes SET outcome=? WHERE token=?", (outcome, token))

    def report(self) -> dict[str, object]:
        """Today's used and remaining writes per kind, plus the weekly invite count.

        A limit of 0 means unlimited, so its remaining count is None. Reading
        does not create the database.
        """
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        reset = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time()
        ).astimezone()
        start, end = week_bounds(now)
        week_reset = datetime.combine(
            date.fromisoformat(end), datetime.min.time()
        ).astimezone()
        used_today = {kind: 0 for kind in self.limits}
        used_week = {kind: 0 for kind in self.weekly_limits}
        if self.path.exists():
            with closing(sqlite3.connect(self.path, timeout=30)) as db:
                try:
                    rows = db.execute(
                        "SELECT kind, count(*) FROM writes WHERE day=? GROUP BY kind",
                        (day,),
                    ).fetchall()
                    week_rows = db.execute(
                        """
                        SELECT kind, count(*) FROM writes
                        WHERE day>=? AND day<? GROUP BY kind
                        """,
                        (start, end),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                    week_rows = []
            for kind, count in rows:
                if kind in used_today:
                    used_today[kind] = count
            for kind, count in week_rows:
                if kind in used_week:
                    used_week[kind] = count
        return {
            "day": day,
            "resets_at": reset.isoformat(),
            "week": start,
            "week_resets_at": week_reset.isoformat(),
            "invites": _count(used_today["invite"], self.limits["invite"]),
            "messages": _count(used_today["message"], self.limits["message"]),
            "navigations": _count(used_today["navigation"], self.limits["navigation"]),
            "sales_nav": _count(used_today["sales_nav"], self.limits["sales_nav"]),
            "weekly_invites": _count(used_week["invite"], self.weekly_limits["invite"]),
            "weekly_messages": _count(
                used_week["message"], self.weekly_limits["message"]
            ),
            "state_file": str(self.path),
        }

    def campaign_repository(self) -> ProspectingRepository:
        """Return the campaign repository sharing this limits database."""
        from linkedin_mcp_server.prospecting.repository import ProspectingRepository

        return ProspectingRepository(self.path)

    def _daily_error(self, kind: str, limit: int, now: datetime) -> CodedToolError:
        reset = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time()
        ).astimezone()
        seconds = max(1, int((reset - now).total_seconds()) + 1)
        return CodedToolError(
            f"Daily {kind} limit reached ({limit}); resets at "
            f"{reset.isoformat()}. retry_safe=true",
            ErrorCode.RATE_LIMITED,
            retry_after=seconds,
            structured_content={
                "retry_safe": True,
                "reset_at": reset.isoformat(),
                "window": "day",
                "kind": kind,
            },
        )

    def _weekly_error(self, kind: str, limit: int, now: datetime) -> CodedToolError:
        _start, end = week_bounds(now)
        reset = datetime.combine(
            date.fromisoformat(end), datetime.min.time()
        ).astimezone()
        seconds = max(1, int((reset - now).total_seconds()) + 1)
        return CodedToolError(
            f"Weekly {kind} limit reached ({limit}); resets at "
            f"{reset.isoformat()}. retry_safe=true",
            ErrorCode.RATE_LIMITED,
            retry_after=seconds,
            structured_content={
                "retry_safe": True,
                "reset_at": reset.isoformat(),
                "window": "week",
                "kind": kind,
            },
        )


def _count(used: int, limit: int) -> dict[str, int | None]:
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used) if limit else None,
    }
