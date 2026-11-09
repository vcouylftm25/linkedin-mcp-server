"""Read-only Sales Navigator tools."""

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.sales_nav_read import MAX_SALES_NAV_PAGES
from linkedin_mcp_server.linkedin.sales_nav_filters import (
    FILTER_FIELDS,
    SalesNavFilterError,
    SalesNavFilters,
)
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
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
        title="Resolve Sales Navigator Filters",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_resolve_filters(
        labels: dict[str, list[str]], ctx: Context
    ) -> dict[str, Any]:
        """Resolve free-text labels in Sales Navigator's visible typeaheads.

        Ambiguous labels return candidates for clarification. One uncached batch
        uses one Sales Navigator page visit.
        """
        allowed_types = set(FILTER_FIELDS) | set(FILTER_FIELDS.values())
        unknown = set(labels) - allowed_types
        if unknown:
            raise CodedToolError(
                f"Unknown filter fields: {', '.join(sorted(unknown))}.",
                ErrorCode.INVALID_ARGUMENT,
            )
        if any(
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
            for value in labels.values()
        ):
            raise CodedToolError(
                "Each filter type must map to a list of labels.",
                ErrorCode.INVALID_ARGUMENT,
            )
        extractor = await get_ready_extractor(
            ctx, tool_name="sales_nav_resolve_filters"
        )
        state_db_path = limits.path if limits is not None else ProspectingLimits().path
        return await extractor.sales_nav_resolve_filters(
            labels, state_db_path=str(state_db_path), charge=charge
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Leads by Filters",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"prospecting", "sales_navigator", "search"},
    )
    @tool_errors
    async def sales_nav_search_leads_by_filters(
        filters: dict[str, Any],
        ctx: Context,
        max_results: Annotated[int, Field(ge=1, le=45)] = 15,
    ) -> dict[str, Any]:
        """Search for leads with structured filters instead of a pasted URL.

        Labels resolve through Sales Navigator's visible typeaheads, using the
        locale-aware 30-day cache first. Uncached labels resolve through live
        typeaheads. Ambiguous labels return candidates and stop before
        searching so the caller can ask which one the banker meant. Up to
        ``max_results`` are returned, across at most three result pages.
        """
        allowed = set(SalesNavFilters.__dataclass_fields__)
        unknown = set(filters) - allowed
        if unknown:
            raise CodedToolError(
                f"Unknown filter fields: {', '.join(sorted(unknown))}.",
                ErrorCode.INVALID_ARGUMENT,
            )
        try:
            for key in FILTER_FIELDS.keys() | {"geography_ids"}:
                value = filters.get(key, [])
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise ValueError(f"{key} must be a list of strings.")
            for key in ("changed_jobs_90d", "posted_recently_30d"):
                if filters.get(key) is not None and type(filters[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean or null.")
            if filters.get("keywords") is not None and not isinstance(
                filters["keywords"], str
            ):
                raise ValueError("keywords must be a string or null.")
            model = SalesNavFilters(
                titles=tuple(filters.get("titles", [])),
                keywords=filters.get("keywords"),
                seniority=tuple(filters.get("seniority", [])),
                function=tuple(filters.get("function", [])),
                industries=tuple(filters.get("industries", [])),
                geographies=tuple(filters.get("geographies", [])),
                geography_ids=tuple(filters.get("geography_ids", [])),
                company_headcount=tuple(filters.get("company_headcount", [])),
                changed_jobs_90d=filters.get("changed_jobs_90d"),
                posted_recently_30d=filters.get("posted_recently_30d"),
                years_in_role=tuple(filters.get("years_in_role", [])),
            )
        except (TypeError, ValueError) as exc:
            raise CodedToolError(str(exc), ErrorCode.INVALID_ARGUMENT) from exc
        pages = min(MAX_SALES_NAV_PAGES, (max_results + 14) // 15)
        extractor = await get_ready_extractor(
            ctx, tool_name="sales_nav_search_leads_by_filters"
        )
        try:
            result = await extractor.sales_nav_search_leads_by_filters(
                model,
                max_pages=pages,
                charge=charge,
                state_db_path=str(
                    limits.path if limits is not None else ProspectingLimits().path
                ),
            )
        except SalesNavFilterError as exc:
            raise CodedToolError(str(exc), ErrorCode.INVALID_ARGUMENT) from exc
        if "leads" in result:
            result["leads"] = result["leads"][:max_results]
        references = result.get("references", {})
        if isinstance(references, dict):
            result["references"] = {
                **references,
                "search_results": references.get("search_results", [])[:max_results],
            }
        else:
            # Keep compatibility with an older extractor result while the
            # public result contract uses references grouped by section.
            result["references"] = references[:max_results]
        return result

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
