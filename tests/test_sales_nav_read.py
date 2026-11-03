"""Pure Sales Navigator classification, parsing, and pagination rules."""

import pytest

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


def test_filter_check_requires_every_requested_query_key():
    requested = (
        "https://www.linkedin.com/sales/search/people?query=filters&sessionId=abc"
    )
    assert missing_filter_keys(
        requested,
        "https://www.linkedin.com/sales/search/people?query=encoded-differently",
    ) == ("sessionId",)
    assert (
        missing_filter_keys(
            requested,
            "https://www.linkedin.com/sales/search/people?query=kept&sessionId=fresh",
        )
        == ()
    )


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
