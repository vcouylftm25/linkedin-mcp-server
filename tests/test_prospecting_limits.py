"""Daily write caps have a persistent, browser-free contract."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.prospecting_limits import ProspectingLimits


def test_separate_caps_persist_and_refuse_before_next_action(tmp_path):
    first = ProspectingLimits(1, 2, str(tmp_path))
    first.reserve("invite")
    second = ProspectingLimits(1, 2, str(tmp_path))
    second.reserve("message")
    second.reserve("message")
    with pytest.raises(CodedToolError) as invite:
        second.reserve("invite")
    with pytest.raises(CodedToolError) as message:
        second.reserve("message")
    assert invite.value.code == message.value.code == ErrorCode.RATE_LIMITED
    assert invite.value.retry_after is not None and invite.value.retry_after > 0
    assert "retry_safe=true" in str(invite.value)
    assert invite.value.structured_content is not None
    assert invite.value.structured_content["retry_safe"] is True
    assert datetime.fromisoformat(
        invite.value.structured_content["reset_at"]
    ).date() == datetime.now().astimezone().date() + timedelta(days=1)


def test_release_and_local_day_reset(tmp_path):
    limit = ProspectingLimits(1, 1, str(tmp_path))
    token = limit.reserve("invite")
    limit.release(token)
    limit.reserve("invite")
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    with patch("linkedin_mcp_server.prospecting_limits.datetime") as clock:
        clock.now.return_value = tomorrow
        clock.combine = datetime.combine
        clock.min = datetime.min
        assert limit.reserve("invite")


def test_zero_disables_cap(tmp_path):
    limit = ProspectingLimits(0, 0, str(tmp_path))
    assert limit.reserve("invite") != limit.reserve("invite")


def test_report_counts_today_and_never_creates_state(tmp_path):
    limits = ProspectingLimits(20, 40, str(tmp_path / "state"))
    empty = limits.report()
    assert empty["invites"] == {"used": 0, "limit": 20, "remaining": 20}
    assert empty["messages"] == {"used": 0, "limit": 40, "remaining": 40}
    assert not (tmp_path / "state").exists()

    limits.reserve("invite")
    token = limits.reserve("message")
    limits.reserve("message")
    limits.release(token)
    report = limits.report()
    assert report["invites"] == {"used": 1, "limit": 20, "remaining": 19}
    assert report["messages"] == {"used": 1, "limit": 40, "remaining": 39}
    assert report["day"] == datetime.now().astimezone().date().isoformat()
    assert datetime.fromisoformat(str(report["resets_at"])).date() == (
        datetime.now().astimezone().date() + timedelta(days=1)
    )


def test_report_treats_zero_as_unlimited(tmp_path):
    limits = ProspectingLimits(0, 1, str(tmp_path))
    limits.reserve("invite")
    limits.reserve("message")
    report = limits.report()
    assert report["invites"] == {"used": 1, "limit": 0, "remaining": None}
    assert report["messages"] == {"used": 1, "limit": 1, "remaining": 0}


async def test_get_prospecting_limits_tool_reads_without_a_browser(tmp_path):
    from fastmcp import FastMCP

    from linkedin_mcp_server.tools.prospecting import register_prospecting_tools

    limits = ProspectingLimits(5, 7, str(tmp_path))
    limits.reserve("invite")
    mcp = FastMCP("t")
    register_prospecting_tools(mcp, limits=limits)
    tool = await mcp.get_tool("get_prospecting_limits")
    result = await tool.fn()
    assert result["enabled"] is True
    assert result["invites"] == {"used": 1, "limit": 5, "remaining": 4}
    assert result["messages"] == {"used": 0, "limit": 7, "remaining": 7}

    unconfigured = FastMCP("u")
    register_prospecting_tools(unconfigured)
    tool = await unconfigured.get_tool("get_prospecting_limits")
    assert (await tool.fn())["enabled"] is False
