"""Prospecting campaigns.

The domain module is pure. Storage, caps, and the batch runner live beside it
and are the only layers allowed to touch SQLite. Nothing in this package sends
an invitation or a message.
"""

from linkedin_mcp_server.prospecting.domain import (
    PAUSE_FOR,
    Campaign,
    IllegalTransition,
    InvalidLead,
    Lead,
    LeadState,
    PauseReason,
)

__all__ = [
    "PAUSE_FOR",
    "Campaign",
    "IllegalTransition",
    "InvalidLead",
    "Lead",
    "LeadState",
    "PauseReason",
]
