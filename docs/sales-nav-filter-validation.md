# Sales Navigator filter validation (first real use)

There is no filter-ID mapping session. The banker supplies ordinary free-text
labels at use time, and `sales_nav_resolve_filters` reads the suggestions shown
by Sales Navigator. The first real use is the validation opportunity: observe
whether the UI produces a unique suggestion, whether ambiguity is returned
without guessing, and whether every chosen filter remains in the final search
URL. Do not save a search, add a lead, or change an account setting.

## Before the first use

- The banker has Sales Navigator open and can run a people search.
- Use the normal request, for example: “diretores financeiros de indústria em
  SP, 500+ funcionários”.
- `sales_nav_search_leads_by_filters` resolves uncached labels through the
  visible typeahead and charges one Sales Navigator navigation for that batch.
  Unique exact matches can proceed; multiple candidates return
  `needs_clarification` for the banker to choose; no candidate means no search.
- `sales_nav_filters.json` may seed known values, but no hand-maintained catalog
  is required.

## Observe and record

1. Record the request's filter types and labels, with account identifiers
   removed.
2. For ambiguous labels, confirm the returned candidate IDs and labels match
   what the UI rendered. The Claude asks the banker which candidate they mean;
   it does not choose one.
3. After a resolved search, compare the requested IDs in the Rest.li `query`
   with the final URL. A missing type or ID must fail closed; the corresponding
   cached mapping is invalidated for the next use.
4. Record the UI locale, whether suggestions were unique or ambiguous, and any
   mismatch between the visible selection and the final URL.
5. Keep any capture anonymized. The committed DOM fixture is explicitly
   synthetic and must be replaced by an anonymized capture when one is approved
   and available.

This observation validates the first real use. It does not create a continuing
manual mapping task or require catalog entries to unlock later searches.
