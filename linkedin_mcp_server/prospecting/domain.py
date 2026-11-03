"""Campaign and lead state, with no browser, database, or framework imports.

A lead that might already have been submitted is ``*_unconfirmed``. Nothing
from that state is allowed to become sendable again: a resumed batch reconciles
by reading what happened, and a path back to ``invite_approved`` or
``message_approved`` would send the same invitation twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

PAUSE_FOR = timedelta(hours=24)

_VANITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,98}[A-Za-z0-9]$")


class LeadState(StrEnum):
    IMPORTED = "imported"
    INVITE_APPROVED = "invite_approved"
    INVITE_UNCONFIRMED = "invite_unconfirmed"
    INVITED = "invited"
    ALREADY_CONNECTED = "already_connected"
    ALREADY_PENDING = "already_pending"
    INCOMING_REQUEST = "incoming_request"
    CONNECT_UNAVAILABLE = "connect_unavailable"
    FOLLOW_ONLY = "follow_only"
    ACCEPTED = "accepted"
    MESSAGE_DRAFTED = "message_drafted"
    MESSAGE_APPROVED = "message_approved"
    MESSAGE_UNCONFIRMED = "message_unconfirmed"
    MESSAGED = "messaged"
    REPLY_UNCONFIRMED = "reply_unconfirmed"
    REPLIED = "replied"
    SKIPPED = "skipped"
    WITHDRAWN = "withdrawn"


class PauseReason(StrEnum):
    RATE_LIMITED = "rate_limited"
    CHECKPOINT = "checkpoint"
    WEEKLY_LIMIT = "weekly_limit"
    NOTE_QUOTA = "note_quota"


class IllegalTransition(Exception):
    """A lead move the campaign is not allowed to make."""

    def __init__(self, src: LeadState, dst: LeadState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"Cannot move a lead from {src.value} to {dst.value}.")


class InvalidLead(Exception):
    """A lead identity this campaign will not store."""


#: States the batch runner may hand to an executor. Leaving one of them is the
#: send. ``*_unconfirmed`` is deliberately absent: the attempt was already
#: committed, and selecting it again would submit it again.
SENDABLE = frozenset({LeadState.INVITE_APPROVED, LeadState.MESSAGE_APPROVED})

#: What an attempt that never submitted is allowed to become. ``invited`` is
#: not here: recording a send that did not happen would free the slot and lie.
NOT_SUBMITTED_STATES = frozenset(
    {
        LeadState.SKIPPED,
        LeadState.ALREADY_CONNECTED,
        LeadState.ALREADY_PENDING,
        LeadState.INCOMING_REQUEST,
        LeadState.CONNECT_UNAVAILABLE,
        LeadState.FOLLOW_ONLY,
    }
)

# ``incoming_request`` has an empty set on purpose. ``connect_with_person``
# accepts an invitation the other person already sent; a campaign must not.
_ALLOWED: dict[LeadState, frozenset[LeadState]] = {
    LeadState.IMPORTED: frozenset(
        {
            LeadState.INVITE_APPROVED,
            LeadState.ALREADY_CONNECTED,
            LeadState.ALREADY_PENDING,
            LeadState.INCOMING_REQUEST,
            LeadState.CONNECT_UNAVAILABLE,
            LeadState.FOLLOW_ONLY,
            LeadState.SKIPPED,
        }
    ),
    LeadState.INVITE_APPROVED: frozenset(
        {
            LeadState.INVITE_UNCONFIRMED,
            LeadState.ALREADY_CONNECTED,
            LeadState.ALREADY_PENDING,
            LeadState.INCOMING_REQUEST,
            LeadState.CONNECT_UNAVAILABLE,
            LeadState.FOLLOW_ONLY,
            LeadState.SKIPPED,
        }
    ),
    LeadState.INVITE_UNCONFIRMED: frozenset(
        {
            LeadState.INVITED,
            LeadState.ACCEPTED,
            LeadState.ALREADY_CONNECTED,
            LeadState.ALREADY_PENDING,
            LeadState.INCOMING_REQUEST,
            LeadState.CONNECT_UNAVAILABLE,
            LeadState.FOLLOW_ONLY,
            LeadState.SKIPPED,
        }
    ),
    LeadState.INVITED: frozenset(
        {LeadState.ACCEPTED, LeadState.WITHDRAWN, LeadState.SKIPPED}
    ),
    LeadState.ALREADY_CONNECTED: frozenset(),
    LeadState.ALREADY_PENDING: frozenset(),
    LeadState.INCOMING_REQUEST: frozenset(),
    LeadState.CONNECT_UNAVAILABLE: frozenset(),
    LeadState.FOLLOW_ONLY: frozenset(),
    LeadState.ACCEPTED: frozenset({LeadState.MESSAGE_DRAFTED, LeadState.SKIPPED}),
    LeadState.MESSAGE_DRAFTED: frozenset(
        {LeadState.MESSAGE_APPROVED, LeadState.SKIPPED}
    ),
    LeadState.MESSAGE_APPROVED: frozenset(
        {LeadState.MESSAGE_UNCONFIRMED, LeadState.SKIPPED}
    ),
    LeadState.MESSAGE_UNCONFIRMED: frozenset({LeadState.MESSAGED, LeadState.SKIPPED}),
    LeadState.MESSAGED: frozenset(
        {LeadState.REPLY_UNCONFIRMED, LeadState.REPLIED, LeadState.SKIPPED}
    ),
    LeadState.REPLY_UNCONFIRMED: frozenset({LeadState.REPLIED, LeadState.MESSAGED}),
    LeadState.REPLIED: frozenset(),
    LeadState.SKIPPED: frozenset(),
    LeadState.WITHDRAWN: frozenset(),
}


def allowed_targets(state: LeadState) -> frozenset[LeadState]:
    """States *state* may move to; a missing entry is a closed door."""
    return _ALLOWED[state]


def transition(src: LeadState, dst: LeadState) -> None:
    """Raise when *src* is not allowed to become *dst*."""
    if dst not in _ALLOWED[src]:
        raise IllegalTransition(src, dst)


def approved_state(action: str) -> LeadState:
    if action == "invite":
        return LeadState.INVITE_APPROVED
    if action == "message":
        return LeadState.MESSAGE_APPROVED
    raise ValueError(f"Unknown prospecting action {action!r}.")


def unconfirmed_state(action: str) -> LeadState:
    if action == "invite":
        return LeadState.INVITE_UNCONFIRMED
    if action == "message":
        return LeadState.MESSAGE_UNCONFIRMED
    raise ValueError(f"Unknown prospecting action {action!r}.")


def approvable_states(action: str) -> frozenset[LeadState]:
    """States that may receive a new approval for *action*.

    The approved state itself is included so a note can be replaced before any
    attempt. The unconfirmed states are excluded: approving them again would
    put a killed attempt back on the queue.
    """
    if action == "invite":
        return frozenset({LeadState.IMPORTED, LeadState.INVITE_APPROVED})
    if action == "message":
        return frozenset({LeadState.MESSAGE_DRAFTED, LeadState.MESSAGE_APPROVED})
    raise ValueError(f"Unknown prospecting action {action!r}.")


def pause_deadline(now: datetime) -> datetime:
    """When a campaign paused at *now* may run again."""
    if now.tzinfo is None:
        raise ValueError("A pause must be computed from a timezone-aware clock.")
    return now + PAUSE_FOR


def blank_text(value: str | None) -> str | None:
    """The text a hash covers: no surrounding space, and no empty string."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def item_hash(
    *,
    lead_vanity: str,
    action: str,
    note: str | None,
    text: str | None,
) -> str:
    """Hash one approved item; the server computes it from the stored fields."""
    payload = {
        "action": action,
        "lead_vanity": lead_vanity,
        "note": blank_text(note),
        "text": blank_text(text),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def batch_hash(item_hashes: list[str]) -> str:
    """Hash of a batch, so one edited item invalidates a client-supplied batch hash."""
    raw = json.dumps(sorted(item_hashes), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def validate_lead_identity(vanity: str, profile_url: str) -> str:
    """Return the canonical profile URL for *vanity*.

    A query or fragment is refused. Sales Navigator session ids travel in the
    query string, and storing one would copy a credential into the campaign file.
    """
    if not isinstance(vanity, str) or _VANITY.fullmatch(vanity) is None:
        raise InvalidLead(
            "lead_vanity must be the public profile slug: 3 to 100 letters, "
            "digits, or hyphens."
        )
    if not isinstance(profile_url, str):
        raise InvalidLead("profile_url must be the https://www.linkedin.com/in/ URL.")
    parsed = urlsplit(profile_url.strip())
    if parsed.scheme != "https" or parsed.netloc != "www.linkedin.com":
        raise InvalidLead("profile_url must be https://www.linkedin.com/in/<vanity>/.")
    if parsed.query or parsed.fragment:
        raise InvalidLead("profile_url must not carry a query or fragment.")
    path = parsed.path.rstrip("/") or "/"
    if path != f"/in/{vanity}":
        raise InvalidLead("profile_url path must be /in/<lead_vanity>/.")
    return f"https://www.linkedin.com/in/{vanity}/"


@dataclass(frozen=True)
class Campaign:
    id: str
    name: str
    created_at: datetime
    filters_json: str | None = None
    paused_until: datetime | None = None
    pause_reason: PauseReason | None = None

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and self.paused_until > now


@dataclass(frozen=True)
class Lead:
    campaign_id: str
    lead_vanity: str
    kind: str
    profile_url: str
    name: str
    state: LeadState
    updated_at: datetime
    lead_urn: str | None = None
    title: str | None = None
    company: str | None = None
    note: str | None = None
    message_text: str | None = None


@dataclass(frozen=True)
class PendingSend:
    """One approved item a single runner step may attempt."""

    campaign_id: str
    lead_vanity: str
    lead_kind: str
    action: str
    note: str | None
    text: str | None
    approval_token: str
    item_hash: str
