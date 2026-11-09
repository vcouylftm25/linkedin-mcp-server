"""Sales Navigator capture against synthetic result-page HTML.

These fixtures exercise the reader and generic reference algorithm only. They
make no claim about LinkedIn's current Sales Navigator markup.
"""

from pathlib import Path

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import LinkedInExtractor
from linkedin_mcp_server.linkedin.sales_nav_resolver import (
    SalesNavFilterCache,
    SalesNavFilterResolver,
)
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "linkedin" / "sales_nav_results.html"
HTML = FIXTURE.read_text(encoding="utf-8")
TYPEAHEAD_FIXTURE = (
    ROOT / "fixtures" / "linkedin" / "sales_nav_typeahead_synthetic.html"
)
TYPEAHEAD_HTML = TYPEAHEAD_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
async def sales_nav_extractor():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(content_type="text/html", body=HTML),
        )
        try:
            yield LinkedInExtractor(page)
        finally:
            await browser.close()


async def test_status_reads_the_synthetic_sales_nav_surface(sales_nav_extractor):
    result = await sales_nav_extractor.sales_nav_status()

    assert result["surface_reached"] is True
    assert result["status"] == "surface_reached"
    assert "Prospecting results" in result["sections"]["status"]


async def test_lead_search_returns_sales_leads_and_public_vanity_when_present(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_search_leads(
        "https://www.linkedin.com/sales/search/people?query=leads"
    )

    assert result["sections"]["search_results"].startswith("Prospecting results")
    assert result["leads"] == [
        {
            "name": "Ada Example",
            "title": "Chief Financial Officer",
            "company": "Northwind Bank",
            "location": "Sao Paulo",
            "sales_lead_url": (
                "/sales/lead/ACwAAASYNTHETIC1,NAME_SEARCH,synthetic-token/"
            ),
            "vanity": "ada-example",
        },
        {
            "name": "Bo Example",
            "title": "Director of Finance",
            "company": "Contoso Payments",
            "location": "Rio de Janeiro",
            "sales_lead_url": (
                "/sales/lead/ACwAAASYNTHETIC2,NAME_SEARCH,synthetic-token-2/"
            ),
            "vanity": None,
        },
    ]
    # This assertion intentionally depends on `/sales/lead/` classification:
    # removing the classifier makes the structural parser drop both leads.
    assert result["references"]["search_results"][0]["kind"] == "sales_lead"


async def test_account_search_returns_a_generic_company_reference(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_search_accounts(
        "https://www.linkedin.com/sales/search/company?query=accounts"
    )

    company = result["references"]["search_results"]
    assert any(
        reference["kind"] == "sales_company"
        and reference["url"] == "/sales/company/900001/"
        for reference in company
    )


async def test_saved_lead_list_returns_rendered_text_and_profile_reference(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_list_leads()

    assert "Executive leadership" in result["sections"]["search_results"]
    assert result["leads"][0]["sales_lead_url"].startswith("/sales/lead/")


async def test_live_filter_resolver_ignores_stale_options_from_an_earlier_query(
    tmp_path,
):
    """Synthetic UI algorithm fixture; replace with anonymized capture when available."""
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(content_type="text/html", body=TYPEAHEAD_HTML),
        )
        await page.goto("https://www.linkedin.com/sales/search/people")
        charges = []
        resolver = SalesNavFilterResolver(
            PageSession(page),
            SalesNavFilterCache(str(tmp_path / "prospecting.sqlite3")),
        )
        try:
            result = await resolver.resolve(
                {"INDUSTRY": ["Indústria de"]}, charge=charges.append
            )
        finally:
            await browser.close()

    assert charges == ["sales_nav"]
    assert result["locale"] == "pt-br"
    assert result["results"][0]["status"] == "resolved"
    assert result["results"][0]["resolved"]["id"] == "fresh-Indústria de"
    assert all(
        item["id"] != "stale-industry" for item in result["results"][0]["candidates"]
    )
