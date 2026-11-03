"""Read-only Sales Navigator page workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.capture import CapturePlan, SectionCapture
from linkedin_mcp_server.linkedin.contracts import ExtractedSection
from linkedin_mcp_server.linkedin.link_metadata import dedupe_references
from linkedin_mcp_server.linkedin.sales_nav_read import (
    MAX_SALES_NAV_PAGES,
    SALES_NAV_NAVIGATION_KIND,
    STATUS_MESSAGE,
    classify_sales_nav_access,
    landed_on_requested_page,
    missing_filter_keys,
    page_of,
    parse_sales_leads,
    require_max_pages,
    url_with_page,
)
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

SALES_NAV_HOME_URL = "https://www.linkedin.com/sales/home"
SALES_NAV_SAVED_LEADS_URL = "https://www.linkedin.com/sales/lists/people/"
SALES_NAV_LEAD_SEARCH_PATH = "/sales/search/people"
SALES_NAV_ACCOUNT_SEARCH_PATH = "/sales/search/company"
MAX_RESULTS_PER_CALL = 15

# The adapter chooses the nearest ancestor containing one lead anchor and
# at least one company link, then copies hrefs and non-anchor text blocks.
# It does not read headings or aria-labels: those are translated.
# `SALES_NAV_RESULT_CARDS` is the marker the policy trace names this
# program by; the trace matches a substring, so the name has to stay.
_SALES_NAV_CARDS_JS = r"""() => {
  const SALES_NAV_RESULT_CARDS = true;
  const leadSelector = 'a[href*="/sales/lead/"]';
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cards = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll(leadSelector)) {
    let card = anchor;
    while (card.parentElement &&
      card.parentElement.querySelectorAll(leadSelector).length === 1 &&
      card.querySelectorAll('a[href*="/sales/company/"]').length === 0) {
      card = card.parentElement;
    }
    if (!card.querySelectorAll('a[href*="/sales/company/"]').length) continue;
    if (seen.has(card)) continue;
    seen.add(card);
    const companies = [...card.querySelectorAll('a[href*="/sales/company/"]')];
    const people = [...card.querySelectorAll('a[href*="/in/"]')];
    const blocks = [];
    for (const node of card.querySelectorAll('*')) {
      if (node.closest('a') || node.children.length) continue;
      const text = clean(node.textContent);
      if (text) blocks.push(text);
    }
    cards.push({
      lead_href: anchor.href,
      lead_text: clean(anchor.textContent),
      company_hrefs: companies.map((item) => item.href),
      company_texts: companies.map((item) => clean(item.textContent)),
      person_hrefs: people.map((item) => item.href),
      text_blocks: blocks,
    });
  }
  return cards;
}"""

NavigationCharge = Callable[[str], object]


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
            "Provide a Sales Navigator URL from "
            f"https://www.linkedin.com{expected_path} with its search query.",
            ErrorCode.INVALID_ARGUMENT,
        )
    return url


def _on_sales_nav_route(url: str, path: str | None = None) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "www.linkedin.com" and (
        parsed.path.startswith("/sales/")
        and (path is None or parsed.path.rstrip("/") == path)
    )


def _checked_max_pages(max_pages: object) -> int:
    try:
        return require_max_pages(max_pages)
    except ValueError as exc:
        raise CodedToolError(str(exc), ErrorCode.INVALID_ARGUMENT) from exc


def _checked_page(url: str) -> int:
    try:
        return page_of(url)
    except ValueError as exc:
        raise CodedToolError(str(exc), ErrorCode.INVALID_ARGUMENT) from exc


class SalesNavigatorReader:
    """Own bounded, rendered-page reads from Sales Navigator."""

    def __init__(self, session: PageSession, capture: SectionCapture) -> None:
        self._session = session
        self._capture = capture

    async def status(
        self, *, charge: NavigationCharge | None = None
    ) -> dict[str, Any]:
        """Check whether the signed-in session can reach Sales Navigator."""
        result = await self._capture_page(
            SALES_NAV_HOME_URL,
            "sales_nav_status",
            CapturePlan(max_scrolls=0),
            charge,
        )
        url = self._session.page.url
        access = classify_sales_nav_access(url)
        return {
            "url": url,
            "sections": {"status": result.text},
            "surface_reached": access == "surface_reached",
            "subscription_status": "unknown",
            "status": access,
            "message": STATUS_MESSAGE[access],
        }

    async def search_leads(
        self,
        search_url: str,
        *,
        max_pages: int = 1,
        charge: NavigationCharge | None = None,
    ) -> dict[str, Any]:
        """Read up to `max_pages` of leads from a Sales Navigator search URL."""
        url = validate_sales_nav_url(search_url, SALES_NAV_LEAD_SEARCH_PATH)
        return await self._read_pages(
            url,
            SALES_NAV_LEAD_SEARCH_PATH,
            "sales_nav_search",
            "leads",
            max_pages=_checked_max_pages(max_pages),
            charge=charge,
        )

    async def search_accounts(
        self,
        search_url: str,
        *,
        max_pages: int = 1,
        charge: NavigationCharge | None = None,
    ) -> dict[str, Any]:
        """Read up to `max_pages` of accounts from a Sales Navigator search URL."""
        url = validate_sales_nav_url(search_url, SALES_NAV_ACCOUNT_SEARCH_PATH)
        return await self._read_pages(
            url,
            SALES_NAV_ACCOUNT_SEARCH_PATH,
            "sales_nav_search",
            "accounts",
            max_pages=_checked_max_pages(max_pages),
            charge=charge,
        )

    async def list_leads(
        self,
        *,
        max_pages: int = 1,
        charge: NavigationCharge | None = None,
    ) -> dict[str, Any]:
        """Read up to `max_pages` of saved leads, if Sales Navigator allows it."""
        return await self._read_pages(
            SALES_NAV_SAVED_LEADS_URL,
            "/sales/lists/people",
            "sales_nav_leads",
            "leads",
            max_pages=_checked_max_pages(max_pages),
            charge=charge,
        )

    async def _read_pages(
        self,
        requested_url: str,
        expected_path: str,
        section_name: str,
        kind: str,
        *,
        max_pages: int,
        charge: NavigationCharge | None,
    ) -> dict[str, Any]:
        start = _checked_page(requested_url)
        seen: set[str] = set()
        leads: list[dict[str, str | None]] = []
        references: list[Any] = []
        texts: list[str] = []
        pages_fetched = 0
        reason = "max_pages"
        next_offset: int | None = start + max_pages
        section_error: dict[str, str] | None = None
        capture_error: dict[str, Any] | None = None

        for index in range(max_pages):
            page_number = start + index
            # The first navigation uses the caller's URL unchanged, so a
            # filter LinkedIn would have kept is not rewritten before the
            # check. Later pages set `page` explicitly. That parameter is
            # not a measured Sales Navigator contract: a landing that does
            # not carry it back, or that repeats the leads already read,
            # stops the walk instead of being treated as the next page.
            page_url = (
                requested_url
                if index == 0
                else url_with_page(requested_url, page_number)
            )
            result = await self._capture_page(
                page_url,
                section_name,
                CapturePlan(max_scrolls=0),
                charge,
            )
            landed = self._session.page.url
            if not _on_sales_nav_route(landed, expected_path):
                raise CodedToolError(
                    "Sales Navigator access was not confirmed for this page. "
                    "Run sales_nav_status and check the URL in Sales Navigator.",
                    ErrorCode.NOT_FOUND,
                )
            missing = missing_filter_keys(requested_url, landed)
            if missing:
                raise CodedToolError(
                    "Sales Navigator dropped requested filter parameters "
                    f"({', '.join(missing)}) from the final URL, so no "
                    "results were returned.",
                    ErrorCode.NOT_FOUND,
                )
            page_leads = (
                await self._lead_cards() if kind == "leads" else []
            )
            ids = (
                [
                    lead["sales_lead_url"]
                    for lead in page_leads
                    if lead["sales_lead_url"]
                ]
                if kind == "leads"
                else [
                    ref["url"]
                    for ref in result.references
                    if ref.get("kind") == "sales_company"
                ]
            )
            fresh = [item for item in ids if item not in seen]
            on_page = landed_on_requested_page(landed, page_number)
            if index > 0 and not on_page:
                section_error = _page_not_advanced(page_number, landed)
                reason = "page_not_advanced"
                next_offset = page_number
                break
            if index > 0 and ids and not fresh:
                section_error = _page_not_advanced(page_number, landed)
                reason = "page_not_advanced"
                next_offset = page_number
                break
            if index == 0 and not on_page:
                raise CodedToolError(
                    "Sales Navigator did not keep the requested page on the "
                    "final URL, so no results were returned.",
                    ErrorCode.NOT_FOUND,
                )
            pages_fetched += 1
            if result.text:
                texts.append(result.text)
            references.extend(result.references)
            for lead in page_leads:
                url = lead["sales_lead_url"]
                if url and url not in seen:
                    leads.append(lead)
            seen.update(item for item in ids if item)
            if result.error and not capture_error:
                capture_error = result.error
            if not ids or result.error:
                # An empty page is the end of the list. An extraction error
                # is not a page the caller should continue from.
                reason = "complete"
                next_offset = None
                break
        else:
            reason = "max_pages"
            next_offset = start + pages_fetched

        # An empty first page is the end of the results, not a stuck page.
        if (
            pages_fetched
            and not seen
            and section_error is None
            and capture_error is None
        ):
            reason = "complete"
            next_offset = None

        cap = MAX_RESULTS_PER_CALL * max_pages
        return self._result(
            ExtractedSection(text="\n".join(texts), references=references),
            requested_url,
            kind,
            leads=leads,
            pagination={
                "pages_fetched": pages_fetched,
                "next_offset": next_offset,
                "reason": reason,
                "result_kind": kind,
            },
            section_error=section_error,
            capture_error=capture_error,
            cap=cap,
        )

    async def _lead_cards(self) -> list[dict[str, str | None]]:
        raw = await self._session.run_on_linkedin(_SALES_NAV_CARDS_JS)
        return parse_sales_leads(raw)

    async def _capture_page(
        self,
        url: str,
        section_name: str,
        plan: CapturePlan,
        charge: NavigationCharge | None,
    ) -> ExtractedSection:
        """Keep a fixed gap before each Sales Navigator navigation."""
        if charge is not None:
            charge(SALES_NAV_NAVIGATION_KIND)
        await self._session.delay(NAV_DELAY)
        return await self._capture.capture(url, section_name, plan)

    @staticmethod
    def _result(
        result: ExtractedSection,
        url: str,
        kind: str,
        *,
        leads: list[dict[str, str | None]] | None = None,
        pagination: dict[str, Any] | None = None,
        section_error: dict[str, str] | None = None,
        capture_error: dict[str, Any] | None = None,
        cap: int = MAX_RESULTS_PER_CALL,
    ) -> dict[str, Any]:
        references = dedupe_references(list(result.references))[:cap]
        output: dict[str, Any] = {
            "url": url,
            "sections": {"search_results": result.text},
            "pagination": pagination
            or {
                "pages_fetched": 1,
                "next_offset": None,
                "reason": "complete",
                "result_kind": kind,
            },
        }
        if references:
            output["references"] = {"search_results": references}
        kept = (leads or [])[:cap]
        if kept:
            output["leads"] = kept
        errors: dict[str, Any] = {}
        if capture_error:
            errors["search_results"] = capture_error
        if section_error:
            errors["search_results"] = section_error
        if not errors and result.error:
            errors["search_results"] = result.error
        if errors:
            output["section_errors"] = errors
        return output


def _page_not_advanced(page_number: int, landed: str) -> dict[str, str]:
    return {
        "error_type": "pagination_stopped",
        "error_message": (
            f"Sales Navigator did not advance to page {page_number} "
            f"(landed on {landed}), so the repeated page was not returned."
        ),
    }
