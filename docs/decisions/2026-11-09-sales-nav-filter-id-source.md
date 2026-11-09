# Resolve Sales Navigator filters from the rendered page

- Date: 2026-11-09
- Supersedes: [2026-11-09-sales-nav-filters-resolve-live.md](2026-11-09-sales-nav-filters-resolve-live.md)

Filter IDs come from the rendered interface only. Read an ID from the visible
suggestion's DOM attributes when present. If no ID is exposed there, select a
single exact suggestion and read its filter ID and display text from the
resulting page URL's Rest.li `query` parameter, using the same parser used to
verify landed searches. Never attach response listeners or inspect page JSON
payloads for filter IDs.

Resolve only one exact label match with a verified ID. Return ambiguous
rendered candidates for clarification. When the interface does not yield an
ID, return `unresolved` and do not build or run a search with a guessed value.
The final search URL must retain every requested filter ID and keyword before
results are returned.
