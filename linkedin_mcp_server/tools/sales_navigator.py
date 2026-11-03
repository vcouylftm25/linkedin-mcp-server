"""Read-only Sales Navigator tools."""

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.sales_nav_read import MAX_SALES_NAV_PAGES
from linkedin_mcp_server.prospecting_limits import ProspectingLimits


def register_sales_navigator_tools(
    mcp: FastMCP,
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    limits: ProspectingLimits | None = None,
) -> None:
    """Register the bounded Sales Navigator reading tools."""

    charge = None if limits is None else limits.reserve

    @mcp.tool(
        timeout=tool_timeout,
        title="Sales Navigator Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator"},
    )
    @tool_errors
    async def sales_nav_status(ctx: Context) -> dict[str, Any]:
        """Check whether the signed-in LinkedIn session reaches Sales Navigator.

        This only confirms whether a Sales Navigator route was reached. It
        cannot confirm the subscription tier or trial dates.
        """
        extractor = await get_ready_extractor(ctx, tool_name="sales_nav_status")
        return await extractor.sales_nav_status(charge=charge)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_search_leads(
        search_url: str,
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=MAX_SALES_NAV_PAGES)] = 1,
    ) -> dict[str, Any]:
        """Read lead results from a Sales Navigator search.

        Pass the URL copied from your Sales Navigator lead search after applying
        filters. ``max_pages`` is capped. Each page is one navigation and counts
        toward the daily Sales Navigator navigation cap. A page that does not
        advance, or a final URL that dropped a requested filter, is not returned
        as a successful filtered page. Leads are structural: name, title,
        company, location, the ``/sales/lead/`` URL, and the public ``/in/``
        vanity when the card has one. It does not save or contact leads.
        """
        extractor = await get_ready_extractor(ctx, tool_name="sales_nav_search_leads")
        return await extractor.sales_nav_search_leads(
            search_url, max_pages=max_pages, charge=charge
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_search_accounts(
        search_url: str,
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=MAX_SALES_NAV_PAGES)] = 1,
    ) -> dict[str, Any]:
        """Read account results from a Sales Navigator search.

        Pass the URL copied from your Sales Navigator account search after
        applying filters. ``max_pages`` is capped. Each page is one navigation.
        A page that does not advance, or a final URL that dropped a requested
        filter, is not returned as a successful filtered page. It does not
        save or contact accounts.
        """
        extractor = await get_ready_extractor(
            ctx, tool_name="sales_nav_search_accounts"
        )
        return await extractor.sales_nav_search_accounts(
            search_url, max_pages=max_pages, charge=charge
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sales Navigator Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator"},
    )
    @tool_errors
    async def sales_nav_list_leads(
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=MAX_SALES_NAV_PAGES)] = 1,
    ) -> dict[str, Any]:
        """Read saved Sales Navigator leads, up to ``max_pages`` pages."""
        extractor = await get_ready_extractor(ctx, tool_name="sales_nav_list_leads")
        return await extractor.sales_nav_list_leads(max_pages=max_pages, charge=charge)
