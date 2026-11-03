"""The only connect call a campaign executor is allowed to make.

``connect_with_person`` defaults to accepting an incoming request. This
function has no parameter that can turn that acceptance back on: it always
passes ``refuse_incoming=True``. A tick also enters
:func:`~linkedin_mcp_server.linkedin.connection.campaign_invite_scope`, so a
direct call that omits the flag raises before it clicks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol


class CampaignConnect(Protocol):
    """A connect callable that cannot default ``refuse_incoming`` to false."""

    def __call__(
        self,
        username: str,
        *,
        note: str | None = ...,
        dry_run: bool = ...,
        refuse_incoming: Literal[True],
    ) -> Any: ...


def perform_campaign_connect(
    connect: CampaignConnect,
    username: str,
    *,
    note: str | None = None,
    dry_run: bool = False,
) -> Any:
    """Call *connect* with the incoming request left unanswered."""
    return connect(username, note=note, dry_run=dry_run, refuse_incoming=True)
