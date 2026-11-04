"""Pure Sales Navigator read decisions.

No browser, no framework. The page adapter collects structure and asks
here what it means. Classification uses the URL and the shape of a result
card, never the words a button or a heading uses, because those words
follow the interface language.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from linkedin_mcp_server.linkedin.link_metadata import classify_link


# One page load of Sales Navigator. `ProspectingLimits` counts this kind
# on its own, apart from invites and messages. The two spellings have to
# stay the same string; the limits test pins them together.
SALES_NAV_NAVIGATION_KIND = "sales_nav"

# Per call, not per day. A caller continues from `next_offset`. Three
# navigations fit a tool timeout; walking Sales Navigator's own 100-page
# ceiling does not.
MAX_SALES_NAV_PAGES = 3

SalesNavAccess = Literal["surface_reached", "login_wall", "paywall", "inconclusive"]

# The same URL prefixes as `_AUTH_BLOCKER_URL_PATTERNS` in `core/auth.py`.
# Duplicated so this module does not import the browser. A test holds the
# two tuples equal.
LOGIN_WALL_PATHS = (
    "/login",
    "/authwall",
    "/checkpoint",
    "/challenge",
    "/uas/login",
    "/uas/consumer-email-challenge",
)

# `/premium` is the upsell path the rest of the server already treats as
# not a profile. `/sales/subscription` is the route this reader already
# treats as leaving a search. Neither is a measured paywall census.
PAYWALL_PATHS = (
    "/premium",
    "/sales/subscription",
)

# Routes that are the product. Anything else under `/sales/` stays
# inconclusive: reaching a sales-shaped path is not the same as reaching
# the product, and an unknown path must not count as access.
PRODUCT_SURFACES = (
    "/sales/home",
    "/sales/search/people",
    "/sales/search/company",
    "/sales/lists/people",
    "/sales/lead",
    "/sales/company",
)

STATUS_MESSAGE = {
    "surface_reached": (
        "The session reached a Sales Navigator route. This does not confirm "
        "an active subscription or trial."
    ),
    "login_wall": (
        "The session landed on a LinkedIn sign-in page. Sales Navigator was not opened."
    ),
    "paywall": (
        "The session landed on a subscription page. Sales Navigator results "
        "were not opened."
    ),
    "inconclusive": (
        "The session did not land on a recognizable Sales Navigator page."
    ),
}


def _path_is(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_linkedin_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def classify_sales_nav_access(url: str) -> SalesNavAccess:
    """Classify a landed URL. The page's words are not an input."""
    parsed = urlparse(url)
    if not _is_linkedin_host(parsed.hostname):
        return "inconclusive"
    path = parsed.path or "/"
    if any(_path_is(path, prefix) for prefix in LOGIN_WALL_PATHS):
        return "login_wall"
    if any(_path_is(path, prefix) for prefix in PAYWALL_PATHS):
        return "paywall"
    if any(_path_is(path, prefix) for prefix in PRODUCT_SURFACES):
        return "surface_reached"
    return "inconclusive"


def require_max_pages(max_pages: object) -> int:
    """Accept 1..MAX_SALES_NAV_PAGES. Booleans are ints and are refused."""
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_SALES_NAV_PAGES:
        raise ValueError(
            f"max_pages must be an integer from 1 to {MAX_SALES_NAV_PAGES}."
        )
    return max_pages


def page_of(url: str) -> int:
    """The 1-based `page` query value, or 1 when the URL does not carry one.

    A present value that is not a positive integer is refused. Guessing
    that it meant the first page would read the wrong results.
    """
    raw = (parse_qs(urlparse(url).query, keep_blank_values=True).get("page") or [None])[
        0
    ]
    if raw is None:
        return 1
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"page must be a positive integer, got {raw!r}.")
    return int(raw)


def url_with_page(url: str, page: int) -> str:
    """Return `url` with its `page` query parameter set.

    Other parameters are kept. Values are re-encoded; callers compare
    keys, not bytes. Page 1 still gets an explicit `page` so a later
    landing can be told apart from a URL that dropped the parameter.
    """
    parsed = urlparse(url)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page"
    ]
    pairs.append(("page", str(page)))
    return urlunparse(parsed._replace(query=urlencode(pairs), fragment=""))


# A Sales Navigator session id rides in the query. It is not a filter:
# a landing that drops it is the same search, and treating the key as one
# refuses that landing.
_NON_FILTER_QUERY_KEYS = frozenset({"sessionId"})


def missing_filter_keys(requested_url: str, landed_url: str) -> tuple[str, ...]:
    """Query keys of `requested_url` that `landed_url` does not carry.

    Presence only. LinkedIn re-encodes values, and a value comparison
    would fail on a search it actually kept. A missing key is a different
    search. ``sessionId`` is not a filter.
    """
    requested = parse_qs(urlparse(requested_url).query, keep_blank_values=True)
    landed = parse_qs(urlparse(landed_url).query, keep_blank_values=True)
    missing = {
        key
        for key in requested
        if key not in landed and key not in _NON_FILTER_QUERY_KEYS
    }
    # Sales Navigator nests every structured filter in the single `query`
    # parameter. Presence of that outer key alone does not prove the filters
    # survived; compare the exact requested IDs after Rest.li decoding.
    requested_query = _restli_query(requested.get("query", [""])[0])
    landed_query = _restli_query(landed.get("query", [""])[0])
    for filter_type, ids in requested_query.items():
        missing_ids = ids - landed_query.get(filter_type, set())
        if missing_ids:
            missing.update(f"{filter_type}:{item}" for item in missing_ids)
    return tuple(sorted(missing))


def _restli_query(value: str) -> dict[str, set[str]]:
    """Read filter type and IDs from a decoded Sales Navigator Rest.li query."""
    value = unquote(value)
    result: dict[str, set[str]] = {}
    marker = "filters:List(("
    start = value.find(marker)
    if start < 0:
        return result
    body = value[start + len(marker) :]
    for item in body.split("),(type:"):
        filter_type = item.split(",values:List((", 1)[0].removeprefix("type:")
        values_at = item.find(",values:List((")
        if values_at < 0:
            continue
        values = item[values_at + len(",values:List((") :]
        ids = set()
        for value_part in values.split("),(id:"):
            if value_part.startswith("id:"):
                ids.add(value_part[3:].split(",", 1)[0].split(")", 1)[0])
        if filter_type and ids:
            result.setdefault(filter_type, set()).update(ids)
    return result


def landed_on_requested_page(landed_url: str, page: int) -> bool:
    """Whether `landed_url` still says it is `page`.

    Page 1 may omit the parameter. Any later page has to carry it, and
    carry that page only. A landing that dropped it is the first page
    again until proven otherwise.
    """
    values = parse_qs(urlparse(landed_url).query).get("page")
    if page == 1 and not values:
        return True
    return values == [str(page)]


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _vanity(url: str) -> str | None:
    classified = classify_link(url)
    if classified is None or classified[0] != "person":
        return None
    slug = classified[1].strip("/")
    prefix, _, vanity = slug.partition("/")
    if prefix != "in" or not vanity or "/" in vanity:
        return None
    return vanity


def parse_sales_leads(cards: object) -> list[dict[str, str | None]]:
    """Turn structural cards into leads.

    A card is one result container the adapter already isolated. `name`
    is the lead anchor's own text. `company` is the text of the single
    `/sales/company/` anchor in that container. `title` and `location`
    are the two text blocks that are not anchors. Any other count, a
    missing name, or an ambiguous company drops the card. `vanity` is the
    public `/in/` slug when the container has exactly one profile link,
    and null otherwise. Nothing here looks for the words Title, Company,
    or Location.
    """
    if not isinstance(cards, list):
        return []
    leads: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for card in cards:
        lead = _one_lead(card)
        url = None if lead is None else lead["sales_lead_url"]
        if lead is None or url is None or url in seen:
            continue
        seen.add(url)
        leads.append(lead)
    return leads


def _one_lead(card: object) -> dict[str, str | None] | None:
    if not isinstance(card, dict):
        return None
    href = card.get("lead_href")
    if not isinstance(href, str):
        return None
    classified = classify_link(href)
    if classified is None or classified[0] != "sales_lead":
        return None
    company_hrefs = _strings(card.get("company_hrefs"))
    company_texts = _strings(card.get("company_texts"))
    person_hrefs = _strings(card.get("person_hrefs"))
    blocks = _strings(card.get("text_blocks"))
    if (
        company_hrefs is None
        or company_texts is None
        or person_hrefs is None
        or blocks is None
        or len(company_hrefs) != len(company_texts)
    ):
        return None

    companies = [
        _clean(text)
        for link, text in zip(company_hrefs, company_texts, strict=True)
        if (kind := classify_link(link)) is not None and kind[0] == "sales_company"
    ]
    vanities = [slug for link in person_hrefs if (slug := _vanity(link))]
    texts = [text for block in blocks if (text := _clean(block))]
    name = _clean(card.get("lead_text"))
    company = companies[0] if len(companies) == 1 else None
    if name is None or company is None or len(texts) != 2:
        return None
    title, location = texts
    return {
        "name": name,
        "title": title,
        "company": company,
        "location": location,
        "sales_lead_url": classified[1],
        "vanity": vanities[0] if len(vanities) == 1 else None,
    }
