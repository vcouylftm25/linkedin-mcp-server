"""Sales Navigator reads are bounded, route-checked, and never submit actions."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.sales_navigator import (
    MAX_RESULTS_PER_CALL,
    SALES_NAV_ACCOUNT_SEARCH_PATH,
    SALES_NAV_LEAD_SEARCH_PATH,
    SALES_NAV_SAVED_LEADS_URL,
    SALES_NAV_HOME_URL,
    SalesNavigatorReader,
    validate_sales_nav_url,
)
from linkedin_mcp_server.linkedin.session import PageSession


LEAD_SEARCH_URL = (
    "https://www.linkedin.com/sales/search/people?query=%28filters%3AList%28%29%29"
)
ACCOUNT_SEARCH_URL = (
    "https://www.linkedin.com/sales/search/company?query=%28filters%3AList%28%29%29"
)


@pytest.mark.parametrize(
    "url,path",
    [
        (LEAD_SEARCH_URL, SALES_NAV_LEAD_SEARCH_PATH),
        (ACCOUNT_SEARCH_URL, SALES_NAV_ACCOUNT_SEARCH_PATH),
    ],
)
def test_accepts_only_expected_sales_navigator_search_route(url, path):
    assert validate_sales_nav_url(url, path) == url


@pytest.mark.parametrize(
    "url,path",
    [
        (
            "http://www.linkedin.com/sales/search/people?query=x",
            SALES_NAV_LEAD_SEARCH_PATH,
        ),
        (
            "https://evil.example/sales/search/people?query=x",
            SALES_NAV_LEAD_SEARCH_PATH,
        ),
        (
            "https://www.linkedin.com/sales/search/company?query=x",
            SALES_NAV_LEAD_SEARCH_PATH,
        ),
        ("https://www.linkedin.com/sales/search/people", SALES_NAV_LEAD_SEARCH_PATH),
        (
            "https://victor@www.linkedin.com/sales/search/people?query=x",
            SALES_NAV_LEAD_SEARCH_PATH,
        ),
    ],
)
def test_rejects_foreign_malformed_or_wrong_type_search_urls(url, path):
    with pytest.raises(CodedToolError) as raised:
        validate_sales_nav_url(url, path)
    assert raised.value.code == ErrorCode.INVALID_ARGUMENT


def _reader(landed_url: str, text: str = "Rendered Sales Navigator results"):
    page = SimpleNamespace(url=landed_url)
    session = SimpleNamespace(page=page, delay=AsyncMock())
    capture = SimpleNamespace(
        capture=AsyncMock(return_value=ExtractedSection(text=text, references=[]))
    )
    return (
        SalesNavigatorReader(
            cast(PageSession, session),
            cast(SectionCapture, capture),
        ),
        capture,
        session,
    )


async def test_status_reports_access_only_when_page_lands_on_sales_route():
    reader, capture, session = _reader("https://www.linkedin.com/sales/home")

    result = await reader.status()

    assert result["surface_reached"] is True
    assert result["status"] == "surface_reached"
    assert result["subscription_status"] == "unknown"
    assert result["url"] == "https://www.linkedin.com/sales/home"
    assert (
        "does not confirm an active subscription or trial"
        in result["sections"]["status"]
    )
    assert capture.capture.await_args.args[:2] == (
        SALES_NAV_HOME_URL,
        "sales_nav_status",
    )
    assert capture.capture.await_args.args[2].max_scrolls == 0
    session.delay.assert_awaited_once_with(2.0)


async def test_lead_search_reads_one_unscrolled_results_page():
    reader, capture, session = _reader("https://www.linkedin.com/sales/search/people")

    result = await reader.search_leads(LEAD_SEARCH_URL)

    assert result["url"] == LEAD_SEARCH_URL
    assert result["sections"]["search_results"] == "Rendered Sales Navigator results"
    assert result["pagination"]["pages_fetched"] == 1
    assert result["pagination"]["reason"] == "single_page"
    assert capture.capture.await_args.args[:2] == (LEAD_SEARCH_URL, "sales_nav_search")
    assert capture.capture.await_args.args[2].max_scrolls == 0
    assert MAX_RESULTS_PER_CALL == 15
    session.delay.assert_awaited_once_with(2.0)


async def test_account_search_rejects_redirect_out_of_sales_nav():
    reader, _capture, _session = _reader("https://www.linkedin.com/sales/subscription")

    with pytest.raises(CodedToolError) as raised:
        await reader.search_accounts(ACCOUNT_SEARCH_URL)

    assert raised.value.code == ErrorCode.NOT_FOUND
    assert "access was not confirmed" in str(raised.value)


async def test_saved_lead_list_is_a_single_read_only_capture():
    reader, capture, session = _reader("https://www.linkedin.com/sales/lists/people/")

    result = await reader.list_leads()

    assert result["sections"]["search_results"] == "Rendered Sales Navigator results"
    assert result["pagination"]["result_kind"] == "leads"
    assert capture.capture.await_args.args[:2] == (
        SALES_NAV_SAVED_LEADS_URL,
        "sales_nav_leads",
    )
    assert capture.capture.await_args.args[2].max_scrolls == 0
    session.delay.assert_awaited_once_with(2.0)


def test_results_cap_links_even_when_the_capture_returns_more():
    references: list[Reference] = [
        {
            "kind": "person",
            "url": f"/in/lead-{index}/",
            "text": f"Lead {index}",
        }
        for index in range(MAX_RESULTS_PER_CALL + 4)
    ]
    result = SalesNavigatorReader._result(
        ExtractedSection(text="Visible results", references=references),
        LEAD_SEARCH_URL,
        "leads",
    )

    assert len(result["references"]["search_results"]) == MAX_RESULTS_PER_CALL
