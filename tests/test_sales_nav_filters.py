from urllib.parse import parse_qs, unquote
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
from pathlib import Path
import tomllib
from typing import cast

import pytest

from linkedin_mcp_server.linkedin.sales_nav_filters import (
    Catalog,
    SalesNavFilterError,
    SalesNavFilters,
    build_sales_nav_search_url,
    normalize_filter_label,
)
from linkedin_mcp_server.linkedin.sales_nav_read import (
    _restli_query,
    missing_filter_keys,
    restli_filter_values,
)
from linkedin_mcp_server.linkedin.sales_nav_resolver import (
    SalesNavFilterCache,
    SalesNavFilterResolver,
    candidate_result,
)
from linkedin_mcp_server.linkedin.session import PageSession


def test_restli_url_round_trip_preserves_unicode_and_delimiters():
    catalog = Catalog(
        version=1,
        entries=(
            {
                "filter_type": "CURRENT_TITLE",
                "id": "title-1",
                "label_pt": "Diretor (M&A), São Paulo: CFO",
                "label_en": "Director (M&A), São Paulo: CFO",
                "source": "synthetic test catalog",
                "verified": False,
            },
        ),
    )
    url = build_sales_nav_search_url(
        SalesNavFilters(
            titles=("Diretor (M&A), São Paulo: CFO",),
            keywords="crédito, (private): equity",
        ),
        catalog=catalog,
    )
    query = parse_qs(url.split("?", 1)[1])["query"][0]

    assert "São Paulo" in unquote(url)
    assert _restli_query(query) == {"CURRENT_TITLE": {"title-1"}}
    assert missing_filter_keys(url, url) == ()
    assert restli_filter_values(url) == [
        {
            "filter_type": "CURRENT_TITLE",
            "id": "title-1",
            "text": "Diretor (M&A), São Paulo: CFO",
        }
    ]
    assert r"Diretor \(M&A\)\, São Paulo\: CFO" in query
    assert r"keywords:crédito\, \(private\)\: equity" in query


def test_filter_seed_is_packaged_and_readable():
    resource = files("linkedin_mcp_server").joinpath("linkedin/sales_nav_filters.json")
    assert json.loads(resource.read_text(encoding="utf-8")) == {
        "version": 1,
        "entries": [],
    }

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert (
        "linkedin/sales_nav_filters.json"
        in pyproject["tool"]["setuptools"]["package-data"]["linkedin_mcp_server"]
    )


def test_unknown_label_fails_closed_and_names_nearby_catalog_options():
    catalog = Catalog(
        version=1,
        entries=(
            {
                "filter_type": "CURRENT_TITLE",
                "id": "title-1",
                "label_pt": "Diretor Financeiro",
                "label_en": "Finance Director",
                "source": "synthetic test catalog",
                "verified": False,
            },
        ),
    )

    with pytest.raises(SalesNavFilterError, match="Diretor Financeiro"):
        build_sales_nav_search_url(
            SalesNavFilters(titles=("Diretor Financeir",)), catalog=catalog
        )


def test_geographies_have_only_the_free_text_label_contract():
    assert "geographies" in SalesNavFilters.__dataclass_fields__
    assert "geography_ids" not in SalesNavFilters.__dataclass_fields__


def test_signals_without_verified_catalog_ids_refuse_to_build():
    with pytest.raises(SalesNavFilterError, match="no verified filter ID"):
        build_sales_nav_search_url(SalesNavFilters(changed_jobs_90d=True))


def test_filter_loss_is_detected_inside_a_still_present_query_parameter():
    requested = (
        "https://www.linkedin.com/sales/search/people?query="
        "%28filters%3AList%28%28type%3AINDUSTRY%2Cvalues%3AList%28"
        "%28id%3A42%2Ctext%3AIndustry%2CselectionType%3AINCLUDED%29%29%29%29"
    )
    landed = (
        "https://www.linkedin.com/sales/search/people?query=%28filters%3AList%28%29%29"
    )

    assert missing_filter_keys(requested, landed) == ("INDUSTRY:42",)


def test_live_candidates_resolve_only_a_unique_exact_label():
    candidates = [
        {"id": "industry-1", "label": "Indústria de transformação"},
        {"id": "industry-2", "label": "Indústria"},
    ]

    resolved = candidate_result("INDUSTRY", " Indústria ", candidates)
    ambiguous = candidate_result("INDUSTRY", "indústria de", candidates)

    assert resolved["status"] == "resolved"
    assert resolved["resolved"]["id"] == "industry-2"
    assert ambiguous["status"] == "ambiguous"
    assert {item["id"] for item in ambiguous["candidates"]} == {
        "industry-1",
        "industry-2",
    }


def test_filter_label_normalization_collapses_whitespace_and_case():
    assert normalize_filter_label("  Vice   President ") == "vice president"
    assert normalize_filter_label("VICE PRESIDENT") == normalize_filter_label(
        "  Vice   President "
    )
    catalog = Catalog(
        version=1,
        entries=(
            {
                "filter_type": "CURRENT_TITLE",
                "id": "title-1",
                "label_pt": "  Vice   President ",
                "label_en": "Vice President",
                "source": "synthetic",
                "verified": False,
            },
        ),
    )
    assert catalog.resolve("CURRENT_TITLE", "Vice President")["id"] == "title-1"


def test_exact_rendered_suggestion_without_id_is_explicitly_unresolved():
    result = candidate_result(
        "CURRENT_TITLE", "Vice President", [{"id": "", "label": "Vice President"}]
    )

    assert result["status"] == "unresolved"
    assert result["resolved"] is None


def test_selected_suggestion_id_and_text_are_read_from_resulting_restli_url():
    from linkedin_mcp_server.linkedin.sales_nav_resolver import _candidate_from_url

    url = build_sales_nav_search_url(
        SalesNavFilters(titles=("Vice President",)),
        catalog=Catalog(
            version=1,
            entries=(
                {
                    "filter_type": "CURRENT_TITLE",
                    "id": "title-1",
                    "label_pt": "Vice President",
                    "label_en": "Vice President",
                    "source": "synthetic",
                    "verified": False,
                },
            ),
        ),
    )
    candidate = _candidate_from_url(
        url,
        {"filter_type": "CURRENT_TITLE", "query": "  Vice   President "},
    )

    assert candidate == {"id": "title-1", "label": "Vice President"}


def test_filter_cache_is_locale_scoped_expires_and_invalidates_missing_ids(tmp_path):
    now = [datetime(2026, 11, 3, tzinfo=timezone.utc)]
    cache = SalesNavFilterCache(
        str(tmp_path / "prospecting.sqlite3"), clock=lambda: now[0]
    )
    value = {"filter_type": "INDUSTRY", "id": "42", "label": "Industry"}
    cache.put("INDUSTRY", " Industry ", "en-US", value)

    assert cache.get("INDUSTRY", "industry", "en-US") == value
    assert cache.get("INDUSTRY", "industry", "pt-BR") is None
    cache.invalidate_ids(("INDUSTRY:42",))
    assert cache.get("INDUSTRY", "industry", "en-US") is None

    cache.put("INDUSTRY", "industry", "en-US", value)
    now[0] += timedelta(days=31)
    assert cache.get("INDUSTRY", "industry", "en-US") is None


async def test_resolver_batches_uncached_labels_into_one_navigation(tmp_path):
    class FakeSession:
        async def run_on_linkedin(self, _script, arg=None):
            if arg is None:
                return "pt-BR"
            return {
                "locale": "pt-BR",
                "options": [
                    {
                        "filter_type": request["filter_type"],
                        "query": request["query"],
                        "candidates": [
                            {"id": request["query"], "label": request["query"]}
                        ],
                    }
                    for request in arg
                ],
            }

    visits = []

    async def navigate(charge):
        charge("sales_nav")
        visits.append("people-search")

    resolver = SalesNavFilterResolver(
        cast(PageSession, FakeSession()),
        SalesNavFilterCache(str(tmp_path / "prospecting.sqlite3")),
        navigate,
    )
    result = await resolver.resolve(
        {"SENIORITY_LEVEL": ["Diretor"], "INDUSTRY": ["Indústria"]},
        charge=visits.append,
    )

    assert visits == ["sales_nav", "people-search"]
    assert [item["status"] for item in result["results"]] == ["resolved", "resolved"]


async def test_resolver_uses_selected_suggestion_url_when_dom_has_no_id(tmp_path):
    url = build_sales_nav_search_url(
        SalesNavFilters(titles=("Vice President",)),
        catalog=Catalog(
            version=1,
            entries=(
                {
                    "filter_type": "CURRENT_TITLE",
                    "id": "title-1",
                    "label_pt": "Vice President",
                    "label_en": "Vice President",
                    "source": "synthetic",
                    "verified": False,
                },
            ),
        ),
    )

    class FakePage:
        url = "https://www.linkedin.com/sales/home"

        def on(self, *_args):
            raise AssertionError("resolver must not observe page responses")

        def remove_listener(self, *_args):
            raise AssertionError("resolver must not observe page responses")

    class FakeSession:
        page = FakePage()

        async def run_on_linkedin(self, _script, arg=None):
            if arg is None:
                return "en-US"
            return {
                "locale": "en-US",
                "options": [
                    {
                        "filter_type": "CURRENT_TITLE",
                        "query": "  Vice   President ",
                        "candidates": [{"id": "", "label": "Vice President"}],
                        "url": url,
                    }
                ],
            }

    resolver = SalesNavFilterResolver(
        cast(PageSession, FakeSession()),
        SalesNavFilterCache(str(tmp_path / "prospecting.sqlite3")),
        navigate=lambda _charge: _noop(),
    )
    result = await resolver.resolve({"CURRENT_TITLE": ["  Vice   President "]})

    assert result["results"][0]["status"] == "resolved"
    assert result["results"][0]["resolved"] == {
        "filter_type": "CURRENT_TITLE",
        "id": "title-1",
        "label": "Vice President",
    }


async def _noop():
    return None
