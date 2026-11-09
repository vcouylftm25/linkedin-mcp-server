"""Pure Sales Navigator classification, parsing, and pagination rules."""

import json
from pathlib import Path

import pytest
from urllib.parse import urlencode

from linkedin_mcp_server.linkedin.sales_nav_read import (
    MAX_SALES_NAV_PAGES,
    classify_sales_nav_access,
    landed_on_requested_page,
    missing_filter_keys,
    parse_sales_leads,
    require_max_pages,
    url_with_page,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.linkedin.com/sales/home", "surface_reached"),
        ("https://www.linkedin.com/sales/search/people", "surface_reached"),
        ("https://www.linkedin.com/login?from=/sales/home", "login_wall"),
        ("https://www.linkedin.com/checkpoint/challenge", "login_wall"),
        ("https://www.linkedin.com/premium/sales", "paywall"),
        ("https://www.linkedin.com/sales/unknown", "inconclusive"),
        ("https://example.com/sales/home", "inconclusive"),
    ],
)
def test_access_classification_uses_landed_url(url, expected):
    assert classify_sales_nav_access(url) == expected


def test_filter_check_ignores_session_id_and_requires_real_filters():
    requested = (
        "https://www.linkedin.com/sales/search/people?query=filters&sessionId=abc"
    )
    assert (
        missing_filter_keys(
            requested,
            "https://www.linkedin.com/sales/search/people?query=encoded-differently",
        )
        == ()
    )
    assert missing_filter_keys(
        requested,
        "https://www.linkedin.com/sales/search/people",
    ) == ("query",)
    assert (
        missing_filter_keys(
            requested,
            "https://www.linkedin.com/sales/search/people?query=kept&sessionId=fresh",
        )
        == ()
    )


@pytest.mark.parametrize(
    ("landed_keywords", "expected"),
    [
        ("keywords:engineer", ()),
        ("keywords:designer", ("keywords",)),
        ("", ("keywords",)),
    ],
)
def test_filter_check_requires_requested_keywords_to_survive(landed_keywords, expected):
    requested = (
        "https://www.linkedin.com/sales/search/people?query=%28keywords%3Aengineer%29"
    )
    landed = "https://www.linkedin.com/sales/search/people?query=" + (
        "%28" + landed_keywords.replace(":", "%3A") + "%29"
        if landed_keywords
        else "%28filters%3AList%28%29%29"
    )

    assert missing_filter_keys(requested, landed) == expected


def test_filter_check_accepts_the_same_restli_keywords_with_other_url_encoding():
    requested = (
        "https://www.linkedin.com/sales/search/people?query="
        "%28filters%3AList%28%29%2Ckeywords%3AS%C3%A3o%20Paulo%5C%2C%20CFO%29"
    )
    landed = "https://www.linkedin.com/sales/search/people?" + urlencode(
        {"query": r"(filters:List(),keywords:São Paulo\, CFO)"}
    )

    assert missing_filter_keys(requested, landed) == ()


def test_page_url_keeps_filters_and_marks_requested_page():
    url = "https://www.linkedin.com/sales/search/people?query=filters&foo=bar"
    page_two = url_with_page(url, 2)
    assert "query=filters" in page_two
    assert "foo=bar" in page_two
    assert landed_on_requested_page(page_two, 2)
    assert not landed_on_requested_page(url, 2)
    assert landed_on_requested_page(url, 1)


@pytest.mark.parametrize("value", [0, -1, True, 1.0, MAX_SALES_NAV_PAGES + 1])
def test_page_budget_refuses_invalid_values(value):
    with pytest.raises(ValueError):
        require_max_pages(value)


def test_structural_lead_parser_returns_public_vanity_or_null_without_labels():
    leads = parse_sales_leads(
        [
            {
                "lead_href": "https://www.linkedin.com/sales/lead/ACwAAA,NAME_SEARCH,x",
                "lead_text": "Ana Silva",
                "company_hrefs": ["https://www.linkedin.com/sales/company/42"],
                "company_texts": ["Banco Exemplo"],
                "person_hrefs": ["https://www.linkedin.com/in/ana-silva/"],
                "text_blocks": ["Diretora Financeira", "São Paulo"],
            },
            {
                "lead_href": "https://www.linkedin.com/sales/lead/ACwBBB,NAME_SEARCH,y",
                "lead_text": "João Souza",
                "company_hrefs": ["https://www.linkedin.com/sales/company/43"],
                "company_texts": ["Empresa Exemplo"],
                "person_hrefs": [],
                "text_blocks": ["CFO", "Recife"],
            },
        ]
    )
    assert leads == [
        {
            "name": "Ana Silva",
            "title": "Diretora Financeira",
            "company": "Banco Exemplo",
            "location": "São Paulo",
            "sales_lead_url": "/sales/lead/ACwAAA,NAME_SEARCH,x/",
            "vanity": "ana-silva",
        },
        {
            "name": "João Souza",
            "title": "CFO",
            "company": "Empresa Exemplo",
            "location": "Recife",
            "sales_lead_url": "/sales/lead/ACwBBB,NAME_SEARCH,y/",
            "vanity": None,
        },
    ]


def test_parser_keeps_cards_with_extra_text_and_without_company_link():
    fixture = Path(__file__).parent / "fixtures" / "sales_nav" / "lead_cards.json"
    cards = json.loads(fixture.read_text(encoding="utf-8"))
    leads = parse_sales_leads(cards)

    assert leads == [
        {
            "name": "Maria Costa",
            "title": "Chief Financial Officer",
            "company": None,
            "location": "São Paulo, Brazil",
            "sales_lead_url": "/sales/lead/ACwCCC,NAME_SEARCH,z/",
            "vanity": "maria-costa",
        },
        {
            "name": "André Lima",
            "title": "Chief Risk Officer",
            "company": "Banco Horizonte",
            "location": "Rio de Janeiro, Brazil",
            "sales_lead_url": "/sales/lead/ACwDDD,NAME_SEARCH,a/",
            "vanity": "andre-lima",
        },
    ]
