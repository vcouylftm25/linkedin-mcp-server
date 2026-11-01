"""Read-only view of the local daily prospecting caps."""

from typing import Any

from fastmcp import FastMCP

from linkedin_mcp_server.dependencies import tool_errors
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
        """Report today's used and remaining connection invites and messages.

        Reads only the local daily counter and the configured caps
        (--daily-invite-limit, --daily-message-limit); it opens no browser and
        sends nothing. Dry runs never count. A limit of 0 means unlimited and
        reports remaining as null. Counts reset at local midnight.
        """
        if limits is None:
            return {
                "enabled": False,
                "message": "Daily prospecting caps are not configured for this server.",
            }
        return {"enabled": True, **limits.report()}
