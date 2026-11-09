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
    _on_sales_nav_route,
    validate_sales_nav_url,
)
from linkedin_mcp_server.linkedin.sales_nav_filters import SalesNavFilters
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
    session = SimpleNamespace(
        page=page,
        delay=AsyncMock(),
        run_on_linkedin=AsyncMock(return_value=[]),
    )
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
    assert result["sections"]["status"] == "Rendered Sales Navigator results"
    assert "does not confirm an active subscription or trial" in result["message"]
    assert capture.capture.await_args.args[:2] == (
        SALES_NAV_HOME_URL,
        "sales_nav_status",
    )
    assert capture.capture.await_args.args[2].max_scrolls == 0
    session.delay.assert_awaited_once_with(2.0)


async def test_lead_search_reads_one_unscrolled_results_page():
    reader, capture, session = _reader(
        "https://www.linkedin.com/sales/search/people?query=%28filters%3AList%28%29%29"
    )

    result = await reader.search_leads(LEAD_SEARCH_URL)

    assert result["url"] == LEAD_SEARCH_URL
    assert result["sections"]["search_results"] == "Rendered Sales Navigator results"
    assert result["pagination"]["pages_fetched"] == 1
    assert result["pagination"]["reason"] == "complete"
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


async def test_saved_lead_list_accepts_a_people_list_identifier():
    reader, _capture, _session = _reader(
        "https://www.linkedin.com/sales/lists/people/123456/"
    )

    result = await reader.list_leads()

    assert result["pagination"]["reason"] == "complete"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/sales/lists/people", True),
        ("/sales/lists/people/123", True),
        ("/sales/lists/people/123/", True),
        ("/sales/lists/people/abc", False),
        ("/sales/lists/people/123/members", False),
    ],
)
def test_saved_list_route_accepts_only_root_or_numeric_list_id(path, expected):
    assert (
        _on_sales_nav_route(f"https://www.linkedin.com{path}", "/sales/lists/people")
        is expected
    )


@pytest.mark.parametrize(
    ("resolution_status", "response_status"),
    [
        ("ambiguous", "needs_clarification"),
        ("unsupported_locale", "unsupported_locale"),
    ],
)
async def test_filter_resolution_keeps_read_tool_response_contract(
    tmp_path, monkeypatch, resolution_status, response_status
):
    url = "https://www.linkedin.com/sales/search/people"
    reader, _capture, session = _reader(url)

    async def resolve(_self, _labels, *, charge=None):
        return {
            "locale": "xx",
            "results": [
                {
                    "filter_type": "CURRENT_TITLE",
                    "query": "Chief Executive Officer",
                    "status": resolution_status,
                    "resolved": None,
                    "candidates": [],
                }
            ],
        }

    monkeypatch.setattr(
        "linkedin_mcp_server.linkedin.sales_navigator.SalesNavFilterResolver.resolve",
        resolve,
    )

    result = await reader.search_leads_by_filters(
        SalesNavFilters(titles=("Chief Executive Officer",)),
        state_db_path=str(tmp_path / "state.sqlite3"),
    )

    assert result == {
        "url": session.page.url,
        "sections": {},
        "status": response_status,
        "filter_resolutions": {
            "locale": "xx",
            "results": [
                {
                    "filter_type": "CURRENT_TITLE",
                    "query": "Chief Executive Officer",
                    "status": resolution_status,
                    "resolved": None,
                    "candidates": [],
                }
            ],
        },
        "pagination": {
            "pages_fetched": 0,
            "next_offset": None,
            "reason": response_status,
            "result_kind": "leads",
        },
    }


async def test_cards_present_but_none_parse_stop_pagination_as_unparsed():
    reader, _capture, session = _reader(
        "https://www.linkedin.com/sales/search/people?query=leads"
    )
    session.run_on_linkedin.return_value = [
        {"lead_href": "/wrong/route", "lead_text": "Card that could not parse"}
    ]

    result = await reader.search_leads(LEAD_SEARCH_URL)

    assert result["pagination"] == {
        "pages_fetched": 1,
        "next_offset": 1,
        "reason": "unparsed_cards",
        "result_kind": "leads",
    }
    assert result["section_errors"]["search_results"]["error_type"] == "unparsed_cards"
    assert result["section_errors"]["search_results"]["code"] == "unparsed_cards"


async def test_reservation_is_released_when_delay_fails_before_navigation(tmp_path):
    from linkedin_mcp_server.prospecting_limits import ProspectingLimits

    page = SimpleNamespace(url="https://www.linkedin.com/sales/home")

    async def fail_delay(_seconds):
        raise RuntimeError("delay failed before navigation")

    session = SimpleNamespace(page=page, delay=fail_delay)
    capture = SimpleNamespace(capture=AsyncMock())
    limits = ProspectingLimits(state_dir=str(tmp_path))
    reader = SalesNavigatorReader(
        cast(PageSession, session), cast(SectionCapture, capture)
    )

    with pytest.raises(RuntimeError, match="delay failed"):
        await reader.status(charge=limits.reserve)

    assert limits.report()["sales_nav"]["used"] == 0
    capture.capture.assert_not_awaited()


async def test_account_search_paginates_with_checked_page_and_charges_each_load():
    page = SimpleNamespace(url="")
    session = SimpleNamespace(page=page, delay=AsyncMock())

    async def capture(url, _section, _plan):
        page.url = url
        page_number = int(url.split("page=")[-1]) if "page=" in url else 1
        return ExtractedSection(
            text=f"Page {page_number}",
            references=[
                {
                    "kind": "sales_company",
                    "url": f"/sales/company/{page_number}/",
                    "context": "Sales Navigator search results",
                }
            ],
        )

    charge = []
    capture_adapter = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    reader = SalesNavigatorReader(
        cast(PageSession, session), cast(SectionCapture, capture_adapter)
    )
    result = await reader.search_accounts(
        ACCOUNT_SEARCH_URL, max_pages=2, charge=charge.append
    )

    assert result["pagination"] == {
        "pages_fetched": 2,
        "next_offset": 3,
        "reason": "max_pages",
        "result_kind": "accounts",
    }
    assert result["sections"]["search_results"] == "Page 1\nPage 2"
    assert charge == ["sales_nav", "sales_nav"]
    assert [call.args[0] for call in capture_adapter.capture.await_args_list] == [
        ACCOUNT_SEARCH_URL,
        ACCOUNT_SEARCH_URL + "&page=2",
    ]


async def test_page_that_did_not_advance_is_excluded_and_returned_as_error():
    reader, capture, _session = _reader(
        "https://www.linkedin.com/sales/search/company?query=filters&page=1"
    )
    capture.capture.side_effect = [
        ExtractedSection(
            text="Page 1",
            references=[
                {
                    "kind": "sales_company",
                    "url": "/sales/company/1/",
                    "context": "Sales Navigator search results",
                }
            ],
        ),
        ExtractedSection(
            text="Repeated page",
            references=[
                {
                    "kind": "sales_company",
                    "url": "/sales/company/1/",
                    "context": "Sales Navigator search results",
                }
            ],
        ),
    ]
    # The mocked browser stayed on page 1 after the requested page 2 load.
    result = await reader.search_accounts(
        "https://www.linkedin.com/sales/search/company?query=filters&page=1",
        max_pages=2,
    )
    assert result["pagination"]["pages_fetched"] == 1
    assert result["pagination"]["next_offset"] == 2
    assert result["pagination"]["reason"] == "page_not_advanced"
    assert result["sections"]["search_results"] == "Page 1"
    assert (
        result["section_errors"]["search_results"]["error_type"] == "pagination_stopped"
    )


async def test_filter_parameter_dropped_by_final_url_fails_closed():
    reader, _capture, _session = _reader(
        "https://www.linkedin.com/sales/search/people?sessionId=fresh"
    )
    with pytest.raises(CodedToolError, match="dropped requested filter parameters"):
        await reader.search_leads(
            "https://www.linkedin.com/sales/search/people?query=filters&sessionId=old"
        )


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
