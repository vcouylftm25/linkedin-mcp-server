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
    """Return outer keys and nested filters or keywords lost on landing.

    LinkedIn re-encodes URL values, so compare decoded Rest.li filter IDs and
    keyword values rather than encoded query bytes. ``sessionId`` is not a
    filter.
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
    # survived; compare the exact requested IDs and keywords after decoding.
    requested_query = _restli_query(requested.get("query", [""])[0])
    landed_query = _restli_query(landed.get("query", [""])[0])
    for filter_type, ids in requested_query.items():
        missing_ids = ids - landed_query.get(filter_type, set())
        if missing_ids:
            missing.update(f"{filter_type}:{item}" for item in missing_ids)
    requested_keywords = _restli_keywords(requested.get("query", [""])[0])
    landed_keywords = _restli_keywords(landed.get("query", [""])[0])
    if requested_keywords is not None and landed_keywords != requested_keywords:
        missing.add("keywords")
    return tuple(sorted(missing))


def _restli_query(value: str) -> dict[str, set[str]]:
    """Read filter type and IDs from a decoded Sales Navigator Rest.li query."""
    value = unquote(value)
    result: dict[str, set[str]] = {}
    marker = "filters:List("
    start = value.find(marker)
    if start < 0:
        return result
    body = _restli_parenthesized(value, start + len(marker) - 1)
    if body is None:
        return result
    for item in _restli_groups(body):
        fields = _split_restli_fields(_restli_group_content(item))
        filter_type = next(
            (field[5:] for field in fields if field.startswith("type:")), ""
        )
        values_field = next(
            (field for field in fields if field.startswith("values:List(")), None
        )
        if not filter_type or values_field is None:
            continue
        values = _restli_parenthesized(values_field, values_field.find("List(") + 4)
        if values is None:
            continue
        ids = set()
        for value_part in _restli_groups(values):
            identifier = next(
                (
                    field[3:]
                    for field in _split_restli_fields(_restli_group_content(value_part))
                    if field.startswith("id:")
                ),
                "",
            )
            if identifier:
                ids.add(_unescape_restli_atom(identifier))
        if ids:
            result.setdefault(filter_type, set()).update(ids)
    return result


def _restli_parenthesized(value: str, opening: int) -> str | None:
    """Return balanced parenthesized content, honoring Rest.li escapes."""
    if opening >= len(value) or value[opening] != "(":
        return None
    depth = 0
    escaped = False
    for index in range(opening, len(value)):
        char = value[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return value[opening + 1 : index]
    return None


def _restli_groups(value: str) -> list[str]:
    """Split a Rest.li list into balanced groups at top-level commas."""
    groups: list[str] = []
    depth = 0
    escaped = False
    start = 0
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            groups.append(value[start:index])
            start = index + 1
    groups.append(value[start:])
    return [group for group in groups if group]


def _restli_group_content(value: str) -> str:
    """Remove one tuple wrapper without trimming nested list delimiters."""
    if value.startswith("(") and value.endswith(")"):
        return value[1:-1]
    return value


def _restli_keywords(value: str) -> str | None:
    """Read the optional keywords atom from a decoded Rest.li query."""
    value = unquote(value)
    marker = "keywords:"
    start = value.find(marker)
    if start < 0:
        return None
    atom = _split_restli_fields(value[start + len(marker) :])[0].rstrip(")")
    return _unescape_restli_atom(atom)


def _unescape_restli_atom(value: str) -> str:
    result: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def _split_restli_fields(value: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    depth = 0
    for char in value:
        if escaped:
            current.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            fields.append("".join(current))
            current.clear()
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def restli_filter_values(url: str) -> list[dict[str, str]]:
    """Read filter IDs and rendered text values from a Sales Navigator URL."""
    query = parse_qs(urlparse(url).query, keep_blank_values=True).get("query", [""])[0]
    query = unquote(query)
    marker = "filters:List("
    start = query.find(marker)
    if start < 0:
        return []
    body = _restli_parenthesized(query, start + len(marker) - 1)
    if body is None:
        return []
    result: list[dict[str, str]] = []
    for item in _restli_groups(body):
        fields = _split_restli_fields(_restli_group_content(item))
        filter_type = next(
            (field[5:] for field in fields if field.startswith("type:")), ""
        )
        values_field = next(
            (field for field in fields if field.startswith("values:List(")), None
        )
        if not filter_type or values_field is None:
            continue
        values = _restli_parenthesized(values_field, values_field.find("List(") + 4)
        if values is None:
            continue
        for value_part in _restli_groups(values):
            value_fields = _split_restli_fields(_restli_group_content(value_part))
            identifier = next(
                (field[3:] for field in value_fields if field.startswith("id:")), ""
            )
            text = next(
                (field[5:] for field in value_fields if field.startswith("text:")), ""
            )
            if identifier and text:
                result.append(
                    {
                        "filter_type": filter_type,
                        "id": _unescape_restli_atom(identifier),
                        "text": _unescape_restli_atom(text),
                    }
                )
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
    is the lead anchor's own text. `company` is optional and comes from the
    first `/sales/company/` anchor. `title` and `location` are the first two
    non-anchor text blocks; extra structural text is tolerated. `vanity` is
    the public `/in/` slug when the container has exactly one profile link,
    and null otherwise. Nothing here looks for translated field labels.
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
    company_hrefs = _strings(card.get("company_hrefs", []))
    company_texts = _strings(card.get("company_texts", []))
    person_hrefs = _strings(card.get("person_hrefs", []))
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
    company = companies[0] if companies else None
    if name is None or len(texts) < 2:
        return None
    title, location = texts[:2]
    return {
        "name": name,
        "title": title,
        "company": company,
        "location": location,
        "sales_lead_url": classified[1],
        "vanity": vanities[0] if len(vanities) == 1 else None,
    }
