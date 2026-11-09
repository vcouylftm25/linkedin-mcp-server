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

    result = await tool_fn({"industries": ["Manufacturing"]}, mock_context)

    assert result == {"status": "ok", "references": []}
    model = extractor.sales_nav_search_leads_by_filters.await_args.args[0]
    assert model.industries == ("Manufacturing",)
    assert model.seniority == ()
    assert extractor.sales_nav_search_leads_by_filters.await_args.kwargs[
        "state_db_path"
    ] == str(limits.path)
