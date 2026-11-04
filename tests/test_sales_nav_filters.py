from urllib.parse import parse_qs, unquote

import pytest

from linkedin_mcp_server.linkedin.sales_nav_filters import (
    Catalog,
    SalesNavFilterError,
    SalesNavFilters,
    build_sales_nav_search_url,
)
from linkedin_mcp_server.linkedin.sales_nav_read import _restli_query
from linkedin_mcp_server.linkedin.sales_nav_read import missing_filter_keys


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
