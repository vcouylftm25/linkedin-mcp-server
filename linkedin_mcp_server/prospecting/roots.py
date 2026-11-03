"""The campaign database has to be the session's database.

A launchd job that opens a different file from the interactive server has its
own caps and its own ``invite_unconfirmed`` rows, so it sends a batch the
server already sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from linkedin_mcp_server.config.schema import ConfigurationError
from linkedin_mcp_server.session_state import auth_root_dir, canonical

if TYPE_CHECKING:
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.prospecting_limits import ProspectingLimits


class ProspectingRootError(ConfigurationError):
    """The configured prospecting directory is not the session directory."""


def resolve_prospecting_root(session_dir: Path, configured: str | None) -> Path:
    """Return *session_dir* when the campaign file is allowed to live there.

    *configured* empty means "use the session directory". Any other directory,
    once both are expanded and resolved, is refused before a file is created.
    """
    session = canonical(session_dir)
    if configured is None or not configured.strip():
        return session
    chosen = canonical(Path(configured))
    if chosen != session:
        raise ProspectingRootError(
            f"Prospecting state directory {chosen} is not the session directory "
            f"{session}. Refusing to open a second database: a scheduled run "
            "would resend a batch this one already sent."
        )
    return session


def prospecting_limits_for(config: AppConfig) -> ProspectingLimits:
    """Caps and campaign storage for this process, in the session directory."""
    from linkedin_mcp_server.prospecting_limits import ProspectingLimits

    root = resolve_prospecting_root(
        auth_root_dir(Path(config.browser.user_data_dir)),
        config.server.prospecting_state_dir,
    )
    return ProspectingLimits(
        config.server.daily_invite_limit,
        config.server.daily_message_limit,
        str(root),
        db_path=str(root / "prospecting.sqlite3"),
    )
