"""Browser-free Sales Navigator filter model and Rest.li URL builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from importlib.resources import files
import json
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class SalesNavFilters:
    """Requested lead filters; labels resolve through the versioned catalog."""

    titles: tuple[str, ...] = ()
    keywords: str | None = None
    seniority: tuple[str, ...] = ()
    function: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()
    geography_ids: tuple[str, ...] = ()
    company_headcount: tuple[str, ...] = ()
    changed_jobs_90d: bool | None = None
    posted_recently_30d: bool | None = None
    years_in_role: tuple[str, ...] = ()


FILTER_FIELDS = {
    "titles": "CURRENT_TITLE",
    "seniority": "SENIORITY_LEVEL",
    "function": "FUNCTION",
    "industries": "INDUSTRY",
    "geographies": "GEO_REGION",
    "company_headcount": "COMPANY_HEADCOUNT",
    "years_in_role": "YEARS_IN_CURRENT_POSITION",
}


class SalesNavFilterError(ValueError):
    """A filter cannot be represented without guessing its LinkedIn ID."""


@dataclass(frozen=True, slots=True)
class Catalog:
    version: int
    entries: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def load(cls) -> Catalog:
        raw = json.loads(
            files("linkedin_mcp_server.linkedin")
            .joinpath("sales_nav_filters.json")
            .read_text(encoding="utf-8")
        )
        return cls(raw["version"], tuple(raw["entries"]))

    def resolve(self, filter_type: str, label: str) -> dict[str, Any]:
        matches = [
            entry
            for entry in self.entries
            if entry["filter_type"] == filter_type
            and label.casefold()
            in (entry["label_pt"].casefold(), entry["label_en"].casefold())
        ]
        if len(matches) == 1:
            return matches[0]
        options = [
            entry["label_pt"]
            for entry in self.entries
            if entry["filter_type"] == filter_type
        ]
        nearby = get_close_matches(label, options, n=5, cutoff=0.25)
        detail = ", ".join(nearby) if nearby else "none in the catalog"
        raise SalesNavFilterError(
            f"Unknown {filter_type} label {label!r}; nearby catalog labels: {detail}."
        )


def build_sales_nav_search_url(
    filters: SalesNavFilters, *, catalog: Catalog | None = None
) -> str:
    """Build a Sales Navigator people search URL using Rest.li query syntax."""
    catalog = catalog or Catalog.load()
    values: list[str] = []
    for field_name, filter_type in FILTER_FIELDS.items():
        labels = getattr(filters, field_name)
        resolved = [catalog.resolve(filter_type, label) for label in labels]
        display_labels = list(labels)
        if field_name == "geographies":
            resolved.extend(
                {
                    "filter_type": filter_type,
                    "id": geo_id,
                    "label_pt": geo_id,
                    "label_en": geo_id,
                    "verified": False,
                    "source": "Raw geography ID supplied by caller; resolver hook pending live measurement.",
                }
                for geo_id in filters.geography_ids
            )
            display_labels.extend(filters.geography_ids)
        if resolved:
            rendered = ",".join(
                f"(id:{_restli_atom(entry['id'])},text:{_restli_atom(label)},selectionType:INCLUDED)"
                for entry, label in zip(resolved, display_labels, strict=True)
            )
            values.append(f"(type:{filter_type},values:List({rendered}))")
    for name, value in (
        ("changed_jobs_90d", filters.changed_jobs_90d),
        ("posted_recently_30d", filters.posted_recently_30d),
    ):
        if value is not None:
            raise SalesNavFilterError(
                f"{name} has no verified filter ID in the catalog; refusing to guess."
            )
    query_parts = [f"filters:List({','.join(values)})"]
    if filters.keywords:
        query_parts.append(f"keywords:{_restli_atom(filters.keywords)}")
    query = f"({','.join(query_parts)})"
    return "https://www.linkedin.com/sales/search/people?query=" + quote(query, safe="")


def _restli_atom(value: str) -> str:
    """Escape delimiters inside an atom before percent-encoding the query."""
    for delimiter in ("\\", "(", ")", ",", ":"):
        value = value.replace(delimiter, "\\" + delimiter)
    return value
