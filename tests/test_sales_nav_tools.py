"""MCP contracts for free-label Sales Navigator filter tools."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.prospecting_limits import ProspectingLimits
from linkedin_mcp_server.tools.sales_navigator import register_sales_navigator_tools


async def test_filtered_search_accepts_only_the_labels_the_banker_supplied(
    tmp_path, mock_context, monkeypatch
):
    extractor = SimpleNamespace(
        sales_nav_search_leads_by_filters=AsyncMock(return_value={"status": "ok"})
    )
    ready = AsyncMock(return_value=extractor)
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.sales_navigator.get_ready_extractor", ready
    )
    limits = ProspectingLimits(2, 2, str(tmp_path))
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp, limits=limits)
    tool = await mcp.get_tool("sales_nav_search_leads_by_filters")
    assert tool is not None
    tool_fn = cast(FunctionTool, tool).fn

    result = await tool_fn(
        {"industries": ["Manufacturing"], "geographies": ["São Paulo"]},
        mock_context,
    )

    assert result == {"status": "ok", "references": {"search_results": []}}
    model = extractor.sales_nav_search_leads_by_filters.await_args.args[0]
    assert model.industries == ("Manufacturing",)
    assert model.geographies == ("São Paulo",)
    assert model.seniority == ()
    assert extractor.sales_nav_search_leads_by_filters.await_args.kwargs[
        "state_db_path"
    ] == str(limits.path)


async def test_filtered_search_caps_leads_and_section_references(
    tmp_path, mock_context, monkeypatch
):
    leads = [{"name": f"Lead {index}"} for index in range(5)]
    references = [
        {"kind": "sales_lead", "url": f"/sales/lead/{index}"} for index in range(5)
    ]
    extractor = SimpleNamespace(
        sales_nav_search_leads_by_filters=AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/sales/search/people?query=x",
                "sections": {"search_results": "five result cards"},
                "leads": leads,
                "references": {"search_results": references},
            }
        )
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.sales_navigator.get_ready_extractor",
        AsyncMock(return_value=extractor),
    )
    mcp = FastMCP("test")
    register_sales_navigator_tools(mcp, limits=ProspectingLimits(2, 2, str(tmp_path)))
    tool = await mcp.get_tool("sales_nav_search_leads_by_filters")

    result = await cast(FunctionTool, tool).fn({}, mock_context, max_results=2)

    assert result["leads"] == leads[:2]
    assert result["references"] == {"search_results": references[:2]}
