"""Daily write caps have a persistent, browser-free contract."""

from datetime import datetime, timedelta, timezone
from typing import cast
import sqlite3
from unittest.mock import patch

import pytest

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.sales_nav_read import SALES_NAV_NAVIGATION_KIND
from linkedin_mcp_server.prospecting_limits import (
    DEFAULT_SALES_NAV_DAILY_NAVIGATIONS,
    ProspectingLimits,
)


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


def test_report_and_reserve_count_unconfirmed_writes_from_other_days(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.prospecting.domain import LeadState
    from linkedin_mcp_server.prospecting.repository import ProspectingRepository

    now = datetime(2026, 11, 3, 12, tzinfo=datetime.now().astimezone().tzinfo)
    limits = ProspectingLimits(1, 1, str(tmp_path))
    repo = ProspectingRepository(limits.path)
    campaign = repo.create_campaign("campaign", now=now)
    repo.add_leads(
        campaign.id,
        [
            {
                "lead_vanity": "alice-smith",
                "profile_url": "https://www.linkedin.com/in/alice-smith/",
                "name": "Alice",
            }
        ],
        now=now,
    )
    token = "unconfirmed-invite"
    with limits._open() as db:
        db.execute(
            "INSERT INTO writes(token, day, kind) VALUES (?, ?, ?)",
            (token, "2026-11-02", "invite"),
        )
        db.execute(
            "UPDATE leads SET state=?, write_token=? WHERE lead_vanity=?",
            (LeadState.INVITE_UNCONFIRMED.value, token, "alice-smith"),
        )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("linkedin_mcp_server.prospecting_limits.datetime", Clock)
    assert limits.report()["invites"] == {"used": 1, "limit": 1, "remaining": 0}
    with limits._open() as db, pytest.raises(CodedToolError):
        db.execute("BEGIN IMMEDIATE")
        limits.insert_reservation(db, "invite", now)


def test_report_does_not_turn_an_unreadable_database_into_zero_usage(tmp_path):
    limits = ProspectingLimits(1, 1, str(tmp_path))
    limits.path.parent.mkdir(parents=True, exist_ok=True)
    limits.path.write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        limits.report()


def test_sales_nav_navigations_have_a_separate_daily_cap(tmp_path):
    limits = ProspectingLimits(1, 1, str(tmp_path), sales_nav_limit=2)
    limits.reserve(SALES_NAV_NAVIGATION_KIND)
    limits.reserve(SALES_NAV_NAVIGATION_KIND)
    with pytest.raises(CodedToolError) as raised:
        limits.reserve(SALES_NAV_NAVIGATION_KIND)
    assert raised.value.code == ErrorCode.RATE_LIMITED
    assert limits.report()["sales_nav"] == {
        "used": 2,
        "limit": 2,
        "remaining": 0,
    }
    assert limits.report()["navigations"] == {
        "used": 0,
        "limit": 100,
        "remaining": 100,
    }
    assert DEFAULT_SALES_NAV_DAILY_NAVIGATIONS == 40
    assert ProspectingLimits(1, 1, str(tmp_path / "default")).limits["sales_nav"] == 40


def test_report_and_reserve_share_the_injected_clock(tmp_path):
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    limits = ProspectingLimits(1, 1, str(tmp_path), clock=lambda: now)

    limits.reserve(SALES_NAV_NAVIGATION_KIND)
    report = limits.report()

    assert report["day"] == "2026-11-03"
    usage = cast(dict[str, int | None], report["sales_nav"])
    assert usage["used"] == 1


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


def test_report_upgrades_database_created_by_filter_cache(tmp_path):
    from linkedin_mcp_server.linkedin.sales_nav_resolver import SalesNavFilterCache

    limits = ProspectingLimits(5, 7, str(tmp_path))
    SalesNavFilterCache(str(limits.path)).put(
        "industries", "Manufacturing", "en-US", {"id": "123", "label": "Manufacturing"}
    )

    report = limits.report()

    assert report["invites"] == {"used": 0, "limit": 5, "remaining": 5}
    assert report["messages"] == {"used": 0, "limit": 7, "remaining": 7}
