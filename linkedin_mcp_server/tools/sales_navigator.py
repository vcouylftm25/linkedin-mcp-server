"""Read-only Sales Navigator tools."""

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors


def register_sales_navigator_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the bounded Sales Navigator reading tools."""

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
        return await extractor.sales_nav_status()

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_search_leads(search_url: str, ctx: Context) -> dict[str, Any]:
        """Read one page of lead results from a Sales Navigator search.

        Pass the URL copied from your Sales Navigator lead search after applying
        filters such as role, seniority, company, industry, geography, job change
        or recent posting. Returns rendered page text and any profile links the
        generic LinkedIn reference reader recognizes; it does not save or contact leads.
        """
        extractor = await get_ready_extractor(ctx, tool_name="sales_nav_search_leads")
        return await extractor.sales_nav_search_leads(search_url)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_search_accounts(
        search_url: str, ctx: Context
    ) -> dict[str, Any]:
        """Read one page of account results from a Sales Navigator search.

        Pass the URL copied from your Sales Navigator account search after
        applying filters such as industry, company size, headquarters, growth
        or buyer intent. It returns rendered text and any recognized company
        references; it does not save or contact accounts.
        """
        extractor = await get_ready_extractor(
            ctx, tool_name="sales_nav_search_accounts"
        )
        return await extractor.sales_nav_search_accounts(search_url)

    @mcp.tool(
        timeout=tool_timeout,
        title="List Sales Navigator Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator"},
    )
    @tool_errors
    async def sales_nav_list_leads(ctx: Context) -> dict[str, Any]:
        """Read the first page of your saved Sales Navigator leads."""
        extractor = await get_ready_extractor(ctx, tool_name="sales_nav_list_leads")
        return await extractor.sales_nav_list_leads()
