"""Sales Navigator capture against synthetic result-page HTML.

These fixtures exercise the reader and generic reference algorithm only. They
make no claim about LinkedIn's current Sales Navigator markup.
"""

from pathlib import Path

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import LinkedInExtractor

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "linkedin" / "sales_nav_results.html"
HTML = FIXTURE.read_text(encoding="utf-8")


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


async def test_lead_search_returns_rendered_text_and_profile_reference(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_search_leads(
        "https://www.linkedin.com/sales/search/people?query=leads"
    )

    assert result["sections"]["search_results"].startswith("Prospecting results")
    assert result["references"]["search_results"][0] == {
        "kind": "person",
        "url": "/in/lead-one/",
        "text": "Lead One",
        "context": "Sales Navigator search results",
    }


async def test_account_search_returns_a_generic_company_reference(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_search_accounts(
        "https://www.linkedin.com/sales/search/company?query=accounts"
    )

    assert result["references"]["search_results"][1] == {
        "kind": "company",
        "url": "/company/target-bank/",
        "text": "Target Bank",
        "context": "Sales Navigator search results",
    }


async def test_saved_lead_list_returns_rendered_text_and_profile_reference(
    sales_nav_extractor,
):
    result = await sales_nav_extractor.sales_nav_list_leads()

    assert "Executive leadership" in result["sections"]["search_results"]
    assert result["references"]["search_results"][0]["url"] == "/in/lead-one/"
