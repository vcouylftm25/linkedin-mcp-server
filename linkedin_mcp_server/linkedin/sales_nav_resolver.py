"""Live, rendered-page Sales Navigator filter resolution and local cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.linkedin.sales_nav_read import SALES_NAV_NAVIGATION_KIND
from linkedin_mcp_server.linkedin.sales_nav_filters import Catalog, FILTER_FIELDS
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


def normalize_filter_label(label: str) -> str:
    """Normalize whitespace and case for cache keys and exact matching."""
    return " ".join(label.split()).casefold()


def candidate_result(
    filter_type: str, requested: str, candidates: list[dict[str, str]]
) -> dict[str, Any]:
    """Resolve only an exact unique label; return all plausible matches otherwise."""
    wanted = normalize_filter_label(requested)
    plausible = [
        {"filter_type": filter_type, "id": item["id"], "label": item["label"]}
        for item in candidates
        if item.get("id")
        and item.get("label")
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
            {"filter_type": filter_type, "id": item["id"], "label": item["label"]}
            for item in candidates
            if item.get("id") and item.get("label")
        ]
    exact = [c for c in plausible if normalize_filter_label(c["label"]) == wanted]
    return {
        "filter_type": filter_type,
        "query": requested,
        "status": "resolved"
        if len(exact) == 1
        else "ambiguous"
        if plausible
        else "not_found",
        "resolved": exact[0] if len(exact) == 1 else None,
        "candidates": exact if len(exact) == 1 else plausible,
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


# The page reads only rendered options. IDs come from attributes on those
# options; no Sales Navigator endpoint is called by this adapter.
_RESOLVE_UI_JS = r"""async (requests) => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const allText = (node) => clean(node.innerText || node.textContent);
  const panelButton = [...document.querySelectorAll('button,[role=button]')]
    .find((node) => ['All filters', 'Todos os filtros'].includes(allText(node)));
  if (panelButton) panelButton.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const findHeading = (labels) => [...document.querySelectorAll('button,[role=button],h1,h2,h3,[role=heading]')]
    .find((node) => labels.includes(allText(node)));
  const options = [];
  for (const request of requests) {
    const trigger = findHeading(request.ui_labels);
    if (!trigger) { options.push({filter_type: request.filter_type, query: request.query, candidates: []}); continue; }
    trigger.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    const input = [...document.querySelectorAll('input[role=combobox],input[aria-autocomplete]')]
      .find((node) => node.offsetParent !== null);
    if (!input) { options.push({filter_type: request.filter_type, query: request.query, candidates: []}); continue; }
    input.focus();
    input.value = request.query;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    await new Promise(resolve => setTimeout(resolve, 150));
    const candidates = [...document.querySelectorAll('[role=option]')].map(node => ({
      id: node.getAttribute('data-id') || node.getAttribute('data-urn') || node.getAttribute('data-value') || node.getAttribute('value') || '',
      label: allText(node)
    })).filter(item => item.label);
    options.push({filter_type: request.filter_type, query: request.query, candidates});
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
            raw, payloads = await self._read_ui(pending)
            locale = str(raw.get("locale") or locale).casefold()
            for item in raw.get("options", []):
                item["candidates"] = _prefer_response_candidates(
                    item.get("candidates", []), payloads
                )
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

    async def _read_ui(
        self, pending: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[Any]]:
        """Prefer IDs in JSON the page fetched itself, with rendered DOM fallback."""
        page = getattr(self.session, "page", None)
        on = getattr(page, "on", None)
        remove_listener = getattr(page, "remove_listener", None)
        if not callable(on) or not callable(remove_listener):
            return await self.session.run_on_linkedin(_RESOLVE_UI_JS, pending), []

        payloads: list[Any] = []
        tasks: set[asyncio.Task] = set()

        def on_response(response: Any) -> None:
            headers = getattr(response, "headers", {}) or {}
            if "json" not in str(headers.get("content-type", "")).casefold():
                return

            async def read_json() -> None:
                try:
                    payloads.append(await response.json())
                except Exception:
                    return

            task = asyncio.create_task(read_json())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        on("response", on_response)
        try:
            raw = await self.session.run_on_linkedin(_RESOLVE_UI_JS, pending)
            if tasks:
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
        finally:
            remove_listener("response", on_response)
        return raw, payloads

    async def _navigate(self, charge) -> None:
        if self.navigate is not None:
            await self.navigate(charge)
        else:
            if charge:
                charge(SALES_NAV_NAVIGATION_KIND)
            await self.session.page.goto("https://www.linkedin.com/sales/search/people")


def _prefer_response_candidates(
    rendered: list[dict[str, str]], payloads: list[Any]
) -> list[dict[str, str]]:
    """Prefer IDs from page-fetched payloads when they match rendered labels."""
    from_payload: list[dict[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            label = next(
                (
                    value.get(key)
                    for key in ("label", "displayName", "name", "text", "title")
                    if isinstance(value.get(key), str)
                ),
                None,
            )
            identifier = next(
                (
                    value.get(key)
                    for key in ("id", "value", "entityUrn", "urn", "geoId")
                    if isinstance(value.get(key), (str, int))
                ),
                None,
            )
            if label and identifier is not None:
                from_payload.append({"label": label, "id": str(identifier)})
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for payload in payloads:
        walk(payload)
    result = []
    for option in rendered:
        matches = [
            item
            for item in from_payload
            if normalize_filter_label(item["label"])
            == normalize_filter_label(option["label"])
        ]
        ids = {item["id"] for item in matches}
        if len(ids) == 1:
            result.append({"label": option["label"], "id": next(iter(ids))})
        elif option.get("id"):
            result.append(option)
    return result
