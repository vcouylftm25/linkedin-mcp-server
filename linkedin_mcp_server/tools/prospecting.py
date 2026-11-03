"""Local prospecting caps and browser-free campaign controls."""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP

from linkedin_mcp_server.dependencies import tool_errors
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.prospecting.domain import InvalidLead
from linkedin_mcp_server.prospecting.export import to_csv, to_json
from linkedin_mcp_server.prospecting.repository import (
    ApprovalRejected,
    ProspectingRepository,
    UnknownCampaign,
)
from linkedin_mcp_server.prospecting_limits import ProspectingLimits


def register_prospecting_tools(
    mcp: FastMCP, *, limits: ProspectingLimits | None = None
) -> None:
    """Register the prospecting-limits tool with the MCP server."""

    @mcp.tool(
        title="Get Prospecting Limits",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    async def get_prospecting_limits() -> dict[str, Any]:
        """Report local daily caps and the active weekly prospecting cap.

        Reads only the local daily counter and the configured caps
        (--daily-invite-limit, --daily-message-limit); it opens no browser and
        sends nothing. It includes profile navigation and weekly invite counts.
        A limit of 0 means unlimited and reports remaining as null.
        """
        if limits is None:
            return {
                "enabled": False,
                "message": "Daily prospecting caps are not configured for this server.",
            }
        return {"enabled": True, **limits.report()}

    repository = limits.campaign_repository() if limits is not None else None

    @mcp.tool(
        title="Create Campaign",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def create_campaign(
        name: str, filters_json: str | None = None
    ) -> dict[str, Any]:
        """Create a local prospecting campaign. This never opens LinkedIn."""
        store = _require_repository(repository)
        campaign = store.create_campaign(name, filters_json)
        return {
            "id": campaign.id,
            "name": campaign.name,
            "created_at": campaign.created_at.isoformat(),
        }

    @mcp.tool(
        title="Add Campaign Leads",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def add_leads(
        campaign_id: str, leads: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Add public profile vanity URLs to a campaign; no browser is used."""
        return _require_repository(repository).add_leads(campaign_id, leads)

    @mcp.tool(
        title="Preview Campaign Batch",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def preview_batch(campaign_id: str, action: str) -> dict[str, Any]:
        """Show the exact item hashes that an explicit approval would cover."""
        return _require_repository(repository).preview_batch(campaign_id, action)

    @mcp.tool(
        title="Approve Campaign Batch",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def approve_batch(
        campaign_id: str,
        action: str,
        items: list[dict[str, Any]],
        items_hash: str | None = None,
    ) -> dict[str, Any]:
        """Approve visible items. The server issues the token and recomputes hashes."""
        return _require_repository(repository).approve_batch(
            campaign_id, action, items, items_hash
        )

    @mcp.tool(
        title="Campaign Status",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def campaign_status(campaign_id: str) -> dict[str, Any]:
        """Report local campaign states, pause and stale approvals."""
        return _require_repository(repository).status(campaign_id)

    @mcp.tool(
        title="Export Campaign",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"prospecting"},
    )
    @tool_errors
    @_campaign_tool_errors
    async def export_campaign(
        campaign_id: str, format: str = "json"
    ) -> dict[str, str]:
        """Export campaign rows as CSV or JSON text; no file is written."""
        store = _require_repository(repository)
        leads = store.list_leads(campaign_id)
        if format == "csv":
            return {"format": format, "content": to_csv(leads)}
        if format == "json":
            return {"format": format, "content": to_json(leads)}
        raise ValueError("format must be csv or json.")


def _require_repository(
    repository: ProspectingRepository | None,
) -> ProspectingRepository:
    if repository is None:
        raise CodedToolError(
            "Campaign tools require configured prospecting storage.",
            ErrorCode.INVALID_ARGUMENT,
        )
    return repository


def _campaign_tool_errors[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Translate domain validation failures into caller-correctable MCP errors."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (ApprovalRejected, InvalidLead, UnknownCampaign, ValueError) as exc:
            raise CodedToolError(str(exc), ErrorCode.INVALID_ARGUMENT) from exc

    return wrapper
