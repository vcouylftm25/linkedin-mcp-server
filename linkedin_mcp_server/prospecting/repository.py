"""SQLite store for campaigns, in the same file as the write caps.

Approvals are consumed one item at a time. Events cannot be updated or deleted;
the triggers in :mod:`linkedin_mcp_server.prospecting.schema` are what make a
direct ``UPDATE`` fail, not the absence of a method.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server.prospecting.domain import (
    Campaign,
    IllegalTransition,
    InvalidLead,
    Lead,
    LeadState,
    PauseReason,
    PendingSend,
    approvable_states,
    approved_state,
    batch_hash,
    blank_text,
    item_hash,
    pause_deadline,
    transition,
    unconfirmed_state,
    validate_lead_identity,
)
from linkedin_mcp_server.prospecting.roots import resolve_prospecting_root
from linkedin_mcp_server.prospecting.schema import ensure_schema

_LEAD_KINDS = frozenset({"invite", "message"})
_NAME_LIMIT = 200


class UnknownCampaign(Exception):
    """No campaign with this id is stored."""


class ApprovalRejected(Exception):
    """The batch was not stored. Nothing was consumed."""


class AttemptConflict(Exception):
    """The lead is no longer the item this step reserved."""


def _now(now: datetime | None) -> datetime:
    if now is None:
        now = datetime.now().astimezone()
    if now.tzinfo is None:
        raise ValueError("Prospecting timestamps must be timezone-aware.")
    return now


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class ProspectingRepository:
    """Campaign rows in the caps database.

    *session_dir*, when given, is the session directory (the parent of the
    browser profile). The database's parent must be that directory. The check
    runs before the file is created.
    """

    def __init__(self, db_path: Path | str, *, session_dir: Path | None = None) -> None:
        self.path = Path(db_path)
        if session_dir is not None:
            resolve_prospecting_root(session_dir, str(self.path.parent))

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        ensure_schema(db)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_campaign(
        self,
        name: str,
        filters_json: str | None = None,
        *,
        now: datetime | None = None,
    ) -> Campaign:
        cleaned = name.strip()
        if not cleaned or len(cleaned) > _NAME_LIMIT:
            raise InvalidLead("Campaign name must be 1 to 200 characters.")
        if filters_json is not None:
            try:
                parsed = json.loads(filters_json)
            except json.JSONDecodeError as exc:
                raise InvalidLead("filters_json must be JSON.") from exc
            if not isinstance(parsed, (dict, list)):
                raise InvalidLead("filters_json must be a JSON object or array.")
        moment = _now(now)
        campaign_id = "cmp_" + uuid.uuid4().hex
        campaign = Campaign(
            id=campaign_id,
            name=cleaned,
            created_at=moment,
            filters_json=filters_json,
        )
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO campaigns(id, name, filters_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (campaign.id, campaign.name, campaign.filters_json, _iso(moment)),
            )
            self._event(
                db,
                campaign.id,
                None,
                "campaign_created",
                {"name": campaign.name},
                moment,
            )
        return campaign

    def add_leads(
        self,
        campaign_id: str,
        leads: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> dict[str, list[Any]]:
        """Commit each lead separately, so one rejected row does not drop the rest."""
        moment = _now(now)
        added: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict[str, str]] = []
        self._require_campaign(campaign_id)
        for raw in leads:
            vanity = str(raw.get("lead_vanity", ""))
            try:
                self._add_one(campaign_id, raw, moment)
            except InvalidLead as exc:
                rejected.append({"lead_vanity": vanity, "error": str(exc)})
            except sqlite3.IntegrityError:
                duplicates.append(vanity)
            else:
                added.append(vanity)
        return {"added": added, "duplicates": duplicates, "rejected": rejected}

    def _add_one(
        self, campaign_id: str, raw: Mapping[str, Any], moment: datetime
    ) -> None:
        vanity = raw.get("lead_vanity")
        profile_url = raw.get("profile_url")
        name = raw.get("name")
        if not isinstance(vanity, str) or not isinstance(profile_url, str):
            raise InvalidLead("lead_vanity and profile_url are required.")
        if not isinstance(name, str) or not name.strip():
            raise InvalidLead("name is required.")
        kind = raw.get("kind", "invite")
        if kind not in _LEAD_KINDS:
            raise InvalidLead("kind must be invite or message.")
        canonical_url = validate_lead_identity(vanity, profile_url)
        lead_urn = raw.get("lead_urn")
        title = raw.get("title")
        company = raw.get("company")
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
                ).fetchone()
                is None
            ):
                raise UnknownCampaign(f"Unknown campaign {campaign_id}.")
            db.execute(
                """
                INSERT INTO leads(
                    lead_vanity, kind, campaign_id, lead_urn, profile_url, name,
                    title, company, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vanity,
                    kind,
                    campaign_id,
                    lead_urn if isinstance(lead_urn, str) else None,
                    canonical_url,
                    name.strip(),
                    title.strip() if isinstance(title, str) else None,
                    company.strip() if isinstance(company, str) else None,
                    LeadState.IMPORTED.value,
                    _iso(moment),
                ),
            )
            self._event(
                db,
                campaign_id,
                vanity,
                "lead_added",
                {"kind": kind},
                moment,
            )

    def get_campaign(self, campaign_id: str) -> Campaign:
        with closing(self._open()) as db:
            row = db.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
        if row is None:
            raise UnknownCampaign(f"Unknown campaign {campaign_id}.")
        return _campaign(row)

    def get_lead(self, lead_vanity: str, kind: str) -> Lead:
        with closing(self._open()) as db:
            row = db.execute(
                "SELECT * FROM leads WHERE lead_vanity=? AND kind=?",
                (lead_vanity, kind),
            ).fetchone()
        if row is None:
            raise InvalidLead(f"Unknown lead {lead_vanity!r} ({kind}).")
        return _lead(row)

    def list_leads(self, campaign_id: str) -> list[Lead]:
        self._require_campaign(campaign_id)
        with closing(self._open()) as db:
            rows = db.execute(
                """
                SELECT * FROM leads WHERE campaign_id=?
                ORDER BY lead_vanity, kind
                """,
                (campaign_id,),
            ).fetchall()
        return [_lead(row) for row in rows]

    def transition_lead(
        self,
        lead_vanity: str,
        kind: str,
        new_state: LeadState,
        *,
        now: datetime | None = None,
    ) -> Lead:
        moment = _now(now)
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked_lead(db, lead_vanity, kind)
            transition(LeadState(row["state"]), new_state)
            db.execute(
                """
                UPDATE leads SET state=?, updated_at=?
                WHERE lead_vanity=? AND kind=?
                """,
                (new_state.value, _iso(moment), lead_vanity, kind),
            )
            self._event(
                db,
                row["campaign_id"],
                lead_vanity,
                "state_changed",
                {"from": row["state"], "to": new_state.value},
                moment,
            )
        return self.get_lead(lead_vanity, kind)

    def preview_batch(self, campaign_id: str, action: str) -> dict[str, Any]:
        """The items an approval would cover, and the hash the server would store.

        Writes nothing. A caller that sends this hash back with different text
        is rejected, because the server hashes the text it is asked to approve.
        """
        self._require_campaign(campaign_id)
        states = tuple(approvable_states(action))
        with closing(self._open()) as db:
            rows = db.execute(
                f"""
                SELECT * FROM leads
                WHERE campaign_id=? AND state IN ({",".join("?" for _ in states)})
                ORDER BY lead_vanity, kind
                """,
                (campaign_id, *(state.value for state in states)),
            ).fetchall()
        items = [_preview_item(row, action) for row in rows]
        hashes = [item["item_hash"] for item in items]
        return {
            "campaign_id": campaign_id,
            "action": action,
            "items": items,
            "items_hash": batch_hash(hashes),
        }

    def approve_batch(
        self,
        campaign_id: str,
        action: str,
        items: Sequence[Mapping[str, Any]],
        items_hash: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Store a server-issued token and a server-computed hash per item.

        *items_hash*, when the client sends one, has to match the hash of the
        texts in *items*. It is never trusted as the stored value. No token is
        accepted from the client. A failure stores nothing.
        """
        if action not in {"invite", "message"}:
            raise ApprovalRejected("action must be invite or message.")
        if not items:
            raise ApprovalRejected("An approval needs at least one lead.")
        moment = _now(now)
        target = approved_state(action)
        allowed = approvable_states(action)
        prepared: list[tuple[sqlite3.Row, str, str | None, str | None]] = []
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
                ).fetchone()
                is None
            ):
                raise UnknownCampaign(f"Unknown campaign {campaign_id}.")
            seen: set[str] = set()
            for raw in items:
                vanity = raw.get("lead_vanity")
                if not isinstance(vanity, str) or vanity in seen:
                    raise ApprovalRejected("Each lead_vanity must appear once.")
                seen.add(vanity)
                row = db.execute(
                    """
                    SELECT * FROM leads
                    WHERE lead_vanity=? AND campaign_id=?
                    """,
                    (vanity, campaign_id),
                ).fetchone()
                if row is None:
                    raise ApprovalRejected(f"{vanity} is not a lead in this campaign.")
                state = LeadState(row["state"])
                if state not in allowed:
                    raise ApprovalRejected(
                        f"{vanity} is {state.value}; it cannot be approved "
                        f"for {action}."
                    )
                if state is not target and state not in allowed:
                    raise ApprovalRejected(f"{vanity} cannot move to {target.value}.")
                raw_note = raw.get("note")
                raw_text = raw.get("text")
                note = blank_text(raw_note if isinstance(raw_note, str) else None)
                text = blank_text(raw_text if isinstance(raw_text, str) else None)
                if action == "message" and text is None:
                    raise ApprovalRejected(f"{vanity} needs the message text.")
                if state != target:
                    try:
                        transition(state, target)
                    except IllegalTransition as exc:
                        raise ApprovalRejected(str(exc)) from exc
                prepared.append((row, vanity, note, text))
            stored_hashes = [
                item_hash(lead_vanity=vanity, action=action, note=note, text=text)
                for _row, vanity, note, text in prepared
            ]
            digest = batch_hash(stored_hashes)
            if items_hash is not None and items_hash != digest:
                raise ApprovalRejected(
                    "items_hash does not match the texts in this batch. "
                    "Approve the current text; a hash is not reusable after an edit."
                )
            token = "apv_" + uuid.uuid4().hex
            result_items = []
            for (row, vanity, note, text), digest_item in zip(
                prepared, stored_hashes, strict=True
            ):
                state = LeadState(row["state"])
                if state != target:
                    db.execute(
                        """
                        UPDATE leads
                        SET state=?, note=?, message_text=?, updated_at=?
                        WHERE lead_vanity=? AND kind=?
                        """,
                        (
                            target.value,
                            note,
                            text,
                            _iso(moment),
                            vanity,
                            row["kind"],
                        ),
                    )
                else:
                    db.execute(
                        """
                        UPDATE leads
                        SET note=?, message_text=?, updated_at=?
                        WHERE lead_vanity=? AND kind=?
                        """,
                        (note, text, _iso(moment), vanity, row["kind"]),
                    )
                db.execute(
                    """
                    INSERT INTO approvals(
                        token, campaign_id, lead_vanity, lead_kind, action,
                        item_hash, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token,
                        campaign_id,
                        vanity,
                        row["kind"],
                        action,
                        digest_item,
                        _iso(moment),
                    ),
                )
                self._event(
                    db,
                    campaign_id,
                    vanity,
                    "approved",
                    {"action": action, "item_hash": digest_item},
                    moment,
                )
                result_items.append({"lead_vanity": vanity, "item_hash": digest_item})
        return {
            "campaign_id": campaign_id,
            "action": action,
            "approval_token": token,
            "items_hash": digest,
            "items": result_items,
        }

    def next_sendable(self, campaign_id: str, action: str) -> PendingSend | None:
        """The next item whose stored hash still matches the lead.

        A hash that no longer matches is left unconsumed and is not returned,
        so one edited note does not block the rest of the batch and does not
        send the edited text.
        """
        state = approved_state(action)
        with closing(self._open()) as db:
            rows = db.execute(
                """
                SELECT leads.*, approvals.token AS approval_token,
                       approvals.item_hash AS item_hash,
                       approvals.action AS action
                FROM leads
                JOIN approvals
                  ON approvals.lead_vanity = leads.lead_vanity
                 AND approvals.lead_kind = leads.kind
                 AND approvals.campaign_id = leads.campaign_id
                WHERE leads.campaign_id=?
                  AND leads.state=?
                  AND approvals.action=?
                  AND approvals.consumed_at IS NULL
                ORDER BY leads.lead_vanity, approvals.approved_at
                """,
                (campaign_id, state.value, action),
            ).fetchall()
        for row in rows:
            current = item_hash(
                lead_vanity=row["lead_vanity"],
                action=action,
                note=row["note"],
                text=row["message_text"],
            )
            if current != row["item_hash"]:
                continue
            return PendingSend(
                campaign_id=campaign_id,
                lead_vanity=row["lead_vanity"],
                lead_kind=row["kind"],
                action=action,
                note=row["note"],
                text=row["message_text"],
                approval_token=row["approval_token"],
                item_hash=row["item_hash"],
            )
        return None

    def begin_attempt(self, limits: Any, item: PendingSend, now: datetime) -> None:
        """Commit ``*_unconfirmed`` and consume this item before any executor runs.

        The quota rows are inserted in the same transaction. A crash rolls the
        whole attempt back; a crash after this returns leaves the lead
        unconfirmed, with the slot still counted, so the next process cannot
        send it again.
        """
        moment = _now(now)
        destination = unconfirmed_state(item.action)
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked_lead(db, item.lead_vanity, item.lead_kind)
            if row["campaign_id"] != item.campaign_id:
                raise AttemptConflict("The lead is in a different campaign.")
            if LeadState(row["state"]) is not approved_state(item.action):
                raise AttemptConflict(
                    f"{item.lead_vanity} is {row['state']}, not waiting to be sent."
                )
            current = item_hash(
                lead_vanity=item.lead_vanity,
                action=item.action,
                note=row["note"],
                text=row["message_text"],
            )
            if current != item.item_hash:
                raise ApprovalRejected(
                    f"The text for {item.lead_vanity} changed after it was approved."
                )
            approval = db.execute(
                """
                SELECT consumed_at FROM approvals
                WHERE token=? AND lead_vanity=? AND action=?
                """,
                (item.approval_token, item.lead_vanity, item.action),
            ).fetchone()
            if approval is None or approval["consumed_at"] is not None:
                raise AttemptConflict("This approval item is already consumed.")
            transition(LeadState(row["state"]), destination)
            navigation = limits.insert_reservation(db, "navigation", moment)
            write = limits.insert_reservation(db, item.action, moment)
            db.execute(
                """
                UPDATE leads
                SET state=?, navigation_token=?, write_token=?, updated_at=?
                WHERE lead_vanity=? AND kind=?
                """,
                (
                    destination.value,
                    navigation,
                    write,
                    _iso(moment),
                    item.lead_vanity,
                    item.lead_kind,
                ),
            )
            updated = db.execute(
                """
                UPDATE approvals SET consumed_at=?
                WHERE token=? AND lead_vanity=? AND action=? AND consumed_at IS NULL
                """,
                (_iso(moment), item.approval_token, item.lead_vanity, item.action),
            )
            if updated.rowcount != 1:
                raise AttemptConflict("This approval item is already consumed.")
            self._event(
                db,
                item.campaign_id,
                item.lead_vanity,
                "attempt_started",
                {"action": item.action, "to": destination.value},
                moment,
            )

    def finish_attempt(
        self,
        limits: Any,
        item: PendingSend,
        *,
        new_state: LeadState | None,
        outcome: str | None,
        release_slots: bool,
        pause_reason: PauseReason | None,
        now: datetime,
    ) -> datetime | None:
        """Persist the outcome of an attempt that was already marked unconfirmed."""
        moment = _now(now)
        expected = unconfirmed_state(item.action)
        paused_until: datetime | None = None
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked_lead(db, item.lead_vanity, item.lead_kind)
            if LeadState(row["state"]) is not expected:
                raise AttemptConflict(
                    f"{item.lead_vanity} is {row['state']}, not {expected.value}."
                )
            if new_state is not None:
                transition(expected, new_state)
                db.execute(
                    """
                    UPDATE leads SET state=?, updated_at=?
                    WHERE lead_vanity=? AND kind=?
                    """,
                    (new_state.value, _iso(moment), item.lead_vanity, item.lead_kind),
                )
            if release_slots:
                # Releasing a known-unsent write must not erase the profile
                # visit: opening the lead already spent the navigation budget.
                token = row["write_token"]
                if token:
                    limits.release_in(db, token)
                db.execute(
                    """
                    UPDATE leads SET navigation_token=NULL, write_token=NULL
                    WHERE lead_vanity=? AND kind=?
                    """,
                    (item.lead_vanity, item.lead_kind),
                )
            elif outcome is not None and row["write_token"]:
                limits.note_outcome_in(db, row["write_token"], outcome)
            if pause_reason is not None:
                paused_until = pause_deadline(moment)
                db.execute(
                    """
                    UPDATE campaigns SET paused_until=?, pause_reason=? WHERE id=?
                    """,
                    (_iso(paused_until), pause_reason.value, item.campaign_id),
                )
                self._event(
                    db,
                    item.campaign_id,
                    None,
                    "campaign_paused",
                    {
                        "reason": pause_reason.value,
                        "paused_until": _iso(paused_until),
                    },
                    moment,
                )
            self._event(
                db,
                item.campaign_id,
                item.lead_vanity,
                "attempt_finished",
                {
                    "action": item.action,
                    "state": (
                        new_state.value if new_state is not None else expected.value
                    ),
                    "outcome": outcome,
                    "released": release_slots,
                },
                moment,
            )
        return paused_until

    def pause(
        self,
        campaign_id: str,
        reason: PauseReason,
        *,
        now: datetime | None = None,
    ) -> datetime:
        moment = _now(now)
        until = pause_deadline(moment)
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
                ).fetchone()
                is None
            ):
                raise UnknownCampaign(f"Unknown campaign {campaign_id}.")
            db.execute(
                "UPDATE campaigns SET paused_until=?, pause_reason=? WHERE id=?",
                (_iso(until), reason.value, campaign_id),
            )
            self._event(
                db,
                campaign_id,
                None,
                "campaign_paused",
                {"reason": reason.value, "paused_until": _iso(until)},
                moment,
            )
        return until

    def status(
        self, campaign_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        moment = _now(now)
        campaign = self.get_campaign(campaign_id)
        counts = {state.value: 0 for state in LeadState}
        stale = 0
        with closing(self._open()) as db:
            for row in db.execute(
                """
                SELECT state, count(*) AS n FROM leads
                WHERE campaign_id=? GROUP BY state
                """,
                (campaign_id,),
            ):
                counts[row["state"]] = row["n"]
            for action, state in (
                ("invite", LeadState.INVITE_APPROVED),
                ("message", LeadState.MESSAGE_APPROVED),
            ):
                rows = db.execute(
                    """
                    SELECT leads.lead_vanity, leads.note, leads.message_text,
                           approvals.item_hash
                    FROM leads
                    LEFT JOIN approvals
                      ON approvals.lead_vanity = leads.lead_vanity
                     AND approvals.lead_kind = leads.kind
                     AND approvals.campaign_id = leads.campaign_id
                     AND approvals.action = ?
                     AND approvals.consumed_at IS NULL
                    WHERE leads.campaign_id=? AND leads.state=?
                    """,
                    (action, campaign_id, state.value),
                ).fetchall()
                by_vanity: dict[str, list[sqlite3.Row]] = {}
                for row in rows:
                    by_vanity.setdefault(row["lead_vanity"], []).append(row)
                for vanity, group in by_vanity.items():
                    current = item_hash(
                        lead_vanity=vanity,
                        action=action,
                        note=group[0]["note"],
                        text=group[0]["message_text"],
                    )
                    if not any(row["item_hash"] == current for row in group):
                        stale += 1
        return {
            "campaign_id": campaign.id,
            "name": campaign.name,
            "paused": campaign.is_paused(moment),
            "paused_until": (
                campaign.paused_until.isoformat() if campaign.paused_until else None
            ),
            "pause_reason": (
                campaign.pause_reason.value if campaign.pause_reason else None
            ),
            "counts": counts,
            "stale_approvals": stale,
        }

    def approval_rows(self, token: str) -> list[dict[str, Any]]:
        with closing(self._open()) as db:
            rows = db.execute(
                """
                SELECT lead_vanity, action, item_hash, consumed_at
                FROM approvals WHERE token=? ORDER BY lead_vanity
                """,
                (token,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _require_campaign(self, campaign_id: str) -> None:
        with closing(self._open()) as db:
            found = db.execute(
                "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
        if found is None:
            raise UnknownCampaign(f"Unknown campaign {campaign_id}.")

    def _locked_lead(
        self, db: sqlite3.Connection, lead_vanity: str, kind: str
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM leads WHERE lead_vanity=? AND kind=?",
            (lead_vanity, kind),
        ).fetchone()
        if row is None:
            raise InvalidLead(f"Unknown lead {lead_vanity!r} ({kind}).")
        return row

    def _event(
        self,
        db: sqlite3.Connection,
        campaign_id: str,
        lead_vanity: str | None,
        kind: str,
        payload: Mapping[str, Any],
        moment: datetime,
    ) -> None:
        db.execute(
            """
            INSERT INTO events(campaign_id, lead_vanity, kind, payload_json, at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                lead_vanity,
                kind,
                json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
                _iso(moment),
            ),
        )


def _campaign(row: sqlite3.Row) -> Campaign:
    reason = row["pause_reason"]
    return Campaign(
        id=row["id"],
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        filters_json=row["filters_json"],
        paused_until=_parse_time(row["paused_until"]),
        pause_reason=PauseReason(reason) if reason else None,
    )


def _lead(row: sqlite3.Row) -> Lead:
    return Lead(
        campaign_id=row["campaign_id"],
        lead_vanity=row["lead_vanity"],
        kind=row["kind"],
        profile_url=row["profile_url"],
        name=row["name"],
        state=LeadState(row["state"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        lead_urn=row["lead_urn"],
        title=row["title"],
        company=row["company"],
        note=row["note"],
        message_text=row["message_text"],
    )


def _preview_item(row: sqlite3.Row, action: str) -> dict[str, Any]:
    digest = item_hash(
        lead_vanity=row["lead_vanity"],
        action=action,
        note=row["note"],
        text=row["message_text"],
    )
    return {
        "lead_vanity": row["lead_vanity"],
        "kind": row["kind"],
        "name": row["name"],
        "note": row["note"],
        "text": row["message_text"],
        "state": row["state"],
        "item_hash": digest,
    }
