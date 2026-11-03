"""Move unconfirmed leads from a re-read of the profile. No browser.

The reader returns a :data:`~linkedin_mcp_server.linkedin.connection.ConnectionState`
or ``None`` when the profile could not be read. ``connectable`` is the one
positive confirmation that an invitation was not sent: the invite anchor is
still on the page. ``pending`` means it is outstanding. ``already_connected``
means it was accepted. Anything else, including a failed read, leaves the
lead unconfirmed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from linkedin_mcp_server.prospecting.domain import Lead, LeadState, transition


@dataclass(frozen=True)
class Reconciliation:
    lead_vanity: str
    kind: str
    before: LeadState
    after: LeadState


class UnconfirmedStore(Protocol):
    def unconfirmed_leads(self) -> Sequence[Lead]: ...

    def move_lead(self, lead: Lead, new_state: LeadState) -> None: ...


def reconciled_state(state: LeadState, reading: str | None) -> LeadState:
    """The state *reading* justifies, or *state* when it justifies nothing.

    *reading* is a ``detect_connection_state`` result. ``None`` and
    ``unavailable`` are not a confirmation that the invitation was withheld.
    """
    if state is not LeadState.INVITE_UNCONFIRMED:
        return state
    if reading == "pending":
        return LeadState.INVITED
    if reading == "already_connected":
        return LeadState.ACCEPTED
    if reading == "connectable":
        return LeadState.INVITE_APPROVED
    return state


def reconcile_unconfirmed(
    store: UnconfirmedStore,
    reader: Callable[[Lead], str | None],
) -> list[Reconciliation]:
    """Move each unconfirmed lead *reader* can account for.

    *reader* is injected. This function does not open a page.
    """
    changes: list[Reconciliation] = []
    for lead in store.unconfirmed_leads():
        if lead.state not in {
            LeadState.INVITE_UNCONFIRMED,
            LeadState.MESSAGE_UNCONFIRMED,
        }:
            continue
        target = reconciled_state(lead.state, reader(lead))
        if target is lead.state:
            continue
        transition(lead.state, target)
        store.move_lead(lead, target)
        changes.append(
            Reconciliation(lead.lead_vanity, lead.kind, lead.state, target)
        )
    return changes


class RepositoryStore:
    """The campaign database as an :class:`UnconfirmedStore`."""

    def __init__(self, repository: Any, limits: Any, now: datetime) -> None:
        self._repository = repository
        self._limits = limits
        self._now = now

    def unconfirmed_leads(self) -> Sequence[Lead]:
        return self._repository.unconfirmed_leads()

    def move_lead(self, lead: Lead, new_state: LeadState) -> None:
        self._repository.apply_unconfirmed_move(
            self._limits, lead, new_state, now=self._now
        )
