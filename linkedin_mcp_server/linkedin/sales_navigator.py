"""Read-only Sales Navigator page workflows."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.capture import CapturePlan, SectionCapture
from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

SALES_NAV_HOME_URL = "https://www.linkedin.com/sales/home"
SALES_NAV_SAVED_LEADS_URL = "https://www.linkedin.com/sales/lists/people/"
SALES_NAV_LEAD_SEARCH_PATH = "/sales/search/people"
SALES_NAV_ACCOUNT_SEARCH_PATH = "/sales/search/company"
MAX_RESULTS_PER_CALL = 15


def validate_sales_nav_url(url: str, expected_path: str) -> str:
    """Accept a Sales Navigator search URL copied from LinkedIn's own UI."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.linkedin.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != expected_path
        or not parsed.query
        or parsed.fragment
    ):
        raise CodedToolError(
            f"Provide a Sales Navigator URL from https://www.linkedin.com{expected_path} with its search query.",
            ErrorCode.INVALID_ARGUMENT,
        )
    return url


def _on_sales_nav_route(url: str, path: str | None = None) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "www.linkedin.com" and (
        parsed.path.startswith("/sales/")
        and (path is None or parsed.path.rstrip("/") == path)
    )


class SalesNavigatorReader:
    """Own bounded, rendered-page reads from Sales Navigator."""

    def __init__(self, session: PageSession, capture: SectionCapture) -> None:
        self._session = session
        self._capture = capture

    async def status(self) -> dict[str, Any]:
        """Check whether the signed-in session can reach Sales Navigator."""
        await self._capture_page(
            SALES_NAV_HOME_URL,
            "sales_nav_status",
            CapturePlan(max_scrolls=0),
        )
        url = self._session.page.url
        reached = _on_sales_nav_route(url)
        status = "surface_reached" if reached else "not_confirmed"
        message = (
            "The session reached a Sales Navigator route. This does not confirm an active subscription or trial."
            if reached
            else "The session did not land on a Sales Navigator page. Access may be unavailable, or LinkedIn may require another step."
        )
        return {
            "url": url,
            "sections": {"status": message},
            "surface_reached": reached,
            "subscription_status": "unknown",
            "status": status,
            "message": message,
        }

    async def search_leads(self, search_url: str) -> dict[str, Any]:
        """Read one page of leads from a Sales Navigator search URL."""
        return await self._search(search_url, SALES_NAV_LEAD_SEARCH_PATH, "leads")

    async def search_accounts(self, search_url: str) -> dict[str, Any]:
        """Read one page of accounts from a Sales Navigator search URL."""
        return await self._search(search_url, SALES_NAV_ACCOUNT_SEARCH_PATH, "accounts")

    async def list_leads(self) -> dict[str, Any]:
        """Read the first page of saved leads, if Sales Navigator allows it."""
        result = await self._capture_page(
            SALES_NAV_SAVED_LEADS_URL,
            "sales_nav_leads",
            CapturePlan(max_scrolls=0),
        )
        if not _on_sales_nav_route(self._session.page.url, "/sales/lists/people"):
            raise CodedToolError(
                "Sales Navigator saved leads were not available at the expected page. Run sales_nav_status to check access.",
                ErrorCode.NOT_FOUND,
            )
        return self._result(result, SALES_NAV_SAVED_LEADS_URL, "leads")

    async def _search(
        self, search_url: str, expected_path: str, kind: str
    ) -> dict[str, Any]:
        url = validate_sales_nav_url(search_url, expected_path)
        result = await self._capture_page(
            url, "sales_nav_search", CapturePlan(max_scrolls=0)
        )
        if not _on_sales_nav_route(self._session.page.url, expected_path):
            raise CodedToolError(
                "Sales Navigator access was not confirmed for this search. Run sales_nav_status and check the URL in Sales Navigator.",
                ErrorCode.NOT_FOUND,
            )
        return self._result(result, url, kind)

    async def _capture_page(
        self, url: str, section_name: str, plan: CapturePlan
    ) -> ExtractedSection:
        """Keep a fixed gap before each Sales Navigator navigation."""
        await self._session.delay(NAV_DELAY)
        return await self._capture.capture(url, section_name, plan)

    @staticmethod
    def _result(result: ExtractedSection, url: str, kind: str) -> dict[str, Any]:
        references = result.references[:MAX_RESULTS_PER_CALL]
        output: dict[str, Any] = {
            "url": url,
            "sections": {"search_results": result.text},
            "pagination": {
                "pages_fetched": 1,
                "next_offset": None,
                "reason": "single_page",
                "result_kind": kind,
            },
        }
        if references:
            output["references"] = {"search_results": references}
        if result.error:
            output["section_errors"] = {"search_results": result.error}
        return output
