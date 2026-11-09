from urllib.parse import parse_qs, unquote
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from linkedin_mcp_server.linkedin.sales_nav_filters import (
    Catalog,
    SalesNavFilterError,
    SalesNavFilters,
    build_sales_nav_search_url,
)
from linkedin_mcp_server.linkedin.sales_nav_read import _restli_query
from linkedin_mcp_server.linkedin.sales_nav_read import missing_filter_keys
from linkedin_mcp_server.linkedin.sales_nav_resolver import (
    SalesNavFilterCache,
    SalesNavFilterResolver,
    _prefer_response_candidates,
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
    assert r"Diretor \(M&A\)\, São Paulo\: CFO" in query
    assert r"keywords:crédito\, \(private\)\: equity" in query


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


def test_page_fetched_response_id_is_preferred_for_the_rendered_option():
    result = _prefer_response_candidates(
        [{"id": "dom-id", "label": "Indústria"}],
        [{"options": [{"id": "response-id", "label": "Indústria"}]}],
    )

    assert result == [{"id": "response-id", "label": "Indústria"}]


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
