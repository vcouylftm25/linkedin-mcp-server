"""Live, rendered-page Sales Navigator filter resolution and local cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.linkedin.sales_nav_read import SALES_NAV_NAVIGATION_KIND
from linkedin_mcp_server.linkedin.sales_nav_filters import (
    Catalog,
    FILTER_FIELDS,
    normalize_filter_label,
)
from linkedin_mcp_server.linkedin.session import PageSession

CACHE_TTL = timedelta(days=30)

# Filter headings are UI labels, so matching is deliberately limited to the
# account's common English and Brazilian Portuguese locale strings.
FILTER_UI_LABELS = {
    "titles": ("Current job title", "Cargo atual"),
    "seniority": ("Seniority level", "Nível de senioridade"),
    "function": ("Function", "Função"),
    "industries": ("Industry", "Setor"),
    "geographies": ("Geography", "Localidade"),
    "company_headcount": ("Company headcount", "Número de funcionários da empresa"),
    "years_in_role": ("Years in current position", "Anos no cargo atual"),
}


def candidate_result(
    filter_type: str, requested: str, candidates: list[dict[str, str]]
) -> dict[str, Any]:
    """Resolve only an exact unique label; return all plausible matches otherwise."""
    wanted = normalize_filter_label(requested)
    plausible = [
        {
            "filter_type": filter_type,
            "id": item.get("id", ""),
            "label": item["label"],
        }
        for item in candidates
        if item.get("label")
        and (
            wanted in normalize_filter_label(item["label"])
            or normalize_filter_label(item["label"]) in wanted
        )
    ]
    if not plausible:
        # The typeahead has already narrowed these rendered suggestions by the
        # typed query; preserve them for banker clarification instead of
        # discarding labels whose wording differs (for example, "500+" vs a
        # displayed employee range).
        plausible = [
            {
                "filter_type": filter_type,
                "id": item.get("id", ""),
                "label": item["label"],
            }
            for item in candidates
            if item.get("id") and item.get("label")
        ]
    exact = [c for c in plausible if normalize_filter_label(c["label"]) == wanted]
    exact_with_ids = [c for c in exact if c.get("id")]
    if len(exact) == 1 and len(exact_with_ids) == 1:
        status = "resolved"
    elif len(exact) > 1 and any(candidate.get("id") for candidate in exact):
        status = "ambiguous"
    elif not exact and any(candidate.get("id") for candidate in plausible):
        status = "ambiguous"
    elif plausible:
        status = "unresolved"
    else:
        status = "not_found"
    return {
        "filter_type": filter_type,
        "query": requested,
        "status": status,
        "resolved": exact_with_ids[0] if status == "resolved" else None,
        "candidates": exact_with_ids if status == "resolved" else plausible,
    }


class SalesNavFilterCache:
    """SQLite cache stored alongside prospecting writes in the same state root."""

    def __init__(self, path: str, *, clock=None, ttl: timedelta = CACHE_TTL) -> None:
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl = ttl

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("""CREATE TABLE IF NOT EXISTS sales_nav_filter_cache (
            filter_type TEXT NOT NULL, normalized_label TEXT NOT NULL,
            locale TEXT NOT NULL, id TEXT NOT NULL, label TEXT NOT NULL,
            resolved_at TEXT NOT NULL,
            PRIMARY KEY (filter_type, normalized_label, locale))""")
        return db

    def get(self, filter_type: str, label: str, locale: str) -> dict[str, str] | None:
        now = self.clock().astimezone(timezone.utc)
        with self._connect() as db:
            row = db.execute(
                "SELECT id,label,resolved_at FROM sales_nav_filter_cache WHERE filter_type=? AND normalized_label=? AND locale=?",
                (filter_type, normalize_filter_label(label), locale.casefold()),
            ).fetchone()
            if row is None:
                return None
            if now - datetime.fromisoformat(row[2]) >= self.ttl:
                db.execute(
                    "DELETE FROM sales_nav_filter_cache WHERE filter_type=? AND normalized_label=? AND locale=?",
                    (filter_type, normalize_filter_label(label), locale.casefold()),
                )
                return None
            return {"filter_type": filter_type, "id": row[0], "label": row[1]}

    def put(
        self, filter_type: str, query: str, locale: str, candidate: dict[str, str]
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sales_nav_filter_cache VALUES (?,?,?,?,?,?)",
                (
                    filter_type,
                    normalize_filter_label(query),
                    locale.casefold(),
                    candidate["id"],
                    candidate["label"],
                    self.clock().astimezone(timezone.utc).isoformat(),
                ),
            )

    def invalidate_ids(self, missing_filter_keys: tuple[str, ...]) -> None:
        """Drop mappings whose filter ID disappeared from a landed search URL."""
        pairs = [item.split(":", 1) for item in missing_filter_keys if ":" in item]
        if not pairs:
            return
        with self._connect() as db:
            db.executemany(
                "DELETE FROM sales_nav_filter_cache WHERE filter_type=? AND id=?",
                pairs,
            )


# The page reads rendered options. It uses IDs on DOM attributes when present;
# otherwise it selects one exact visible suggestion and reads its resulting URL.
_RESOLVE_UI_JS = r"""async (requests) => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const allText = (node) => clean(node.innerText || node.textContent);
  const headingSelector = 'button,[role=button],h1,h2,h3,[role=heading]';
  const findHeading = (labels, root = document) => [...root.querySelectorAll(headingSelector)]
    .find((node) => labels.includes(allText(node)));
  const hasRequestedHeading = (root) => requests.some(request =>
    findHeading(request.ui_labels, root));
  let panel = [...document.querySelectorAll('[role=dialog],[role=region]')]
    .find(hasRequestedHeading) || null;
  if (!panel) {
    const structuralToggle = [...document.querySelectorAll('button[aria-controls][aria-expanded]')]
      .find(button => {
        const controlled = document.getElementById(button.getAttribute('aria-controls'));
        return controlled && (controlled.matches('[role=dialog],[role=region]') ||
          controlled.querySelector('input[role=combobox],[role=heading]'));
      });
    const localizedFallback = [...document.querySelectorAll('button,[role=button]')]
      .find(node => ['All filters', 'Todos os filtros'].includes(allText(node)));
    const toggle = structuralToggle || localizedFallback;
    if (toggle) {
      toggle.click();
      await new Promise(resolve => setTimeout(resolve, 0));
      panel = [...document.querySelectorAll('[role=dialog],[role=region]')]
        .find(hasRequestedHeading) || null;
    }
  }
  const waitForOptions = async () => {
    let previous = null;
    let stable = 0;
    for (let attempt = 0; attempt < 20; attempt++) {
      const nodes = [...document.querySelectorAll('[role=option], [role=listbox] [role=listitem]')]
        .filter(node => allText(node));
      const signature = nodes.map(allText).join('\u0000');
      if (signature && signature === previous) stable++;
      else stable = 0;
      if (stable >= 1) return nodes;
      previous = signature;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return [...document.querySelectorAll('[role=option], [role=listbox] [role=listitem]')]
      .filter(node => allText(node));
  };
  const options = [];
  for (const request of requests) {
    const trigger = findHeading(request.ui_labels, panel || document);
    if (!trigger) { options.push({filter_type: request.filter_type, query: request.query, candidates: []}); continue; }
    trigger.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    const input = [...document.querySelectorAll('input[role=combobox],input[aria-autocomplete]')]
      .find((node) => node.offsetParent !== null);
    if (!input) { options.push({filter_type: request.filter_type, query: request.query, candidates: []}); continue; }
    input.focus();
    input.value = request.query;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    const nodes = await waitForOptions();
    const candidates = nodes.map(node => ({
      id: node.getAttribute('data-id') || node.getAttribute('data-urn') || node.getAttribute('data-value') || node.getAttribute('value') || '',
      label: allText(node)
    }));
    let url = '';
    const normalize = value => clean(value).toLocaleLowerCase();
    const exact = candidates.filter(item => normalize(item.label) === normalize(request.query));
    if (exact.length === 1 && !exact[0].id) {
      nodes[candidates.indexOf(exact[0])].click();
      await new Promise(resolve => setTimeout(resolve, 0));
      url = window.location.href;
    }
    options.push({filter_type: request.filter_type, query: request.query, candidates, url});
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  }
  return {locale: document.documentElement.lang || 'und', options};
}"""


class SalesNavFilterResolver:
    def __init__(
        self, session: PageSession, cache: SalesNavFilterCache, navigate=None
    ) -> None:
        self.session = session
        self.cache = cache
        self.navigate = navigate

    async def resolve(
        self, labels: dict[str, list[str]], *, charge=None
    ) -> dict[str, Any]:
        cached: dict[tuple[str, str], dict[str, str]] = {}
        seeded: set[tuple[str, str]] = set()
        seed = Catalog.load()
        current_url = getattr(getattr(self.session, "page", None), "url", "")
        did_navigate = False
        host = urlparse(str(current_url)).hostname
        if host not in {"linkedin.com", "www.linkedin.com"}:
            await self._navigate(charge)
            did_navigate = True
        locale = str(
            await self.session.run_on_linkedin(
                "() => document.documentElement.lang || 'und'"
            )
        ).casefold()
        if not locale.startswith(("en", "pt")):
            return {
                "locale": locale,
                "results": [
                    {
                        "filter_type": filter_type,
                        "query": query,
                        "status": "unsupported_locale",
                        "resolved": None,
                        "candidates": [],
                    }
                    for filter_type, queries in labels.items()
                    for query in queries
                ],
            }
        pending = []
        for filter_type, queries in labels.items():
            for query in queries:
                item = self.cache.get(filter_type, query, locale)
                if item:
                    cached[(filter_type, query)] = item
                else:
                    seed_matches = [
                        entry
                        for entry in seed.entries
                        if entry.get("filter_type") == filter_type
                        and normalize_filter_label(query)
                        in {
                            normalize_filter_label(str(entry.get("label_pt", ""))),
                            normalize_filter_label(str(entry.get("label_en", ""))),
                        }
                    ]
                    if len(seed_matches) == 1:
                        entry = seed_matches[0]
                        cached[(filter_type, query)] = {
                            "filter_type": filter_type,
                            "id": str(entry["id"]),
                            "label": str(
                                entry.get("label_pt") or entry.get("label_en")
                            ),
                        }
                        seeded.add((filter_type, query))
                        self.cache.put(
                            filter_type, query, locale, cached[(filter_type, query)]
                        )
                        continue
                    field_name = next(
                        (
                            field
                            for field, kind in FILTER_FIELDS.items()
                            if kind == filter_type
                        ),
                        filter_type,
                    )
                    pending.append(
                        {
                            "filter_type": filter_type,
                            "query": query,
                            "ui_labels": list(FILTER_UI_LABELS[field_name]),
                        }
                    )
        observed: dict[tuple[str, str], dict[str, Any]] = {}
        if pending:
            if not did_navigate:
                await self._navigate(charge)
            raw = await self._read_ui(pending)
            locale = str(raw.get("locale") or locale).casefold()
            for item in raw.get("options", []):
                url_candidate = _candidate_from_url(item.get("url", ""), item)
                if url_candidate is not None and not any(
                    candidate.get("id") for candidate in item.get("candidates", [])
                ):
                    item["candidates"] = [url_candidate]
                result = candidate_result(
                    item["filter_type"], item["query"], item["candidates"]
                )
                observed[(item["filter_type"], item["query"])] = result
                if result["status"] == "resolved":
                    self.cache.put(
                        item["filter_type"], item["query"], locale, result["resolved"]
                    )
        results = []
        for filter_type, queries in labels.items():
            for query in queries:
                cached_item = cached.get((filter_type, query))
                if cached_item:
                    result = {
                        "filter_type": filter_type,
                        "query": query,
                        "status": "resolved",
                        "resolved": cached_item,
                        "candidates": [cached_item],
                        "source": "seed" if (filter_type, query) in seeded else "cache",
                    }
                else:
                    result = observed.get(
                        (filter_type, query),
                        {
                            "filter_type": filter_type,
                            "query": query,
                            "status": "not_found",
                            "resolved": None,
                            "candidates": [],
                        },
                    )
                results.append(result)
        return {"locale": locale, "results": results}

    async def _read_ui(self, pending: list[dict[str, Any]]) -> dict[str, Any]:
        """Read suggestions from the rendered page and its resulting URL only."""
        return await self.session.run_on_linkedin(_RESOLVE_UI_JS, pending)

    async def _navigate(self, charge) -> None:
        if self.navigate is not None:
            await self.navigate(charge)
        else:
            if charge:
                charge(SALES_NAV_NAVIGATION_KIND)
            await self.session.page.goto("https://www.linkedin.com/sales/search/people")


def _candidate_from_url(url: str, item: dict[str, Any]) -> dict[str, str] | None:
    """Resolve an exact selected suggestion from the page's Rest.li URL."""
    from linkedin_mcp_server.linkedin.sales_nav_read import restli_filter_values

    expected_type = item.get("filter_type")
    wanted = normalize_filter_label(str(item.get("query", "")))
    matches = [
        {"id": entry["id"], "label": entry["text"]}
        for entry in restli_filter_values(url)
        if entry["filter_type"] == expected_type
        and normalize_filter_label(entry["text"]) == wanted
    ]
    return matches[0] if len(matches) == 1 else None
