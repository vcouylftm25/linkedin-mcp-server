# Resolve Sales Navigator filter labels at use time

- Date: 2026-11-09
- Supersedes: none

Bankers state prospecting filters in free text at use time. Requiring a
hand-maintained filter-ID catalog would block ordinary requests and create a
second source of truth for values Sales Navigator already renders. Resolve
uncached labels through the visible filter typeahead, in one people-search
page visit per batch. Do not issue private API requests.

Only one exact label match resolves automatically. Return rendered candidates
when the match is ambiguous or non-exact so the banker can clarify. Cache
unique mappings in the existing prospecting SQLite database by filter type,
normalized label, and account locale for 30 days. Invalidate an entry when the
final search URL drops its filter ID. The versioned JSON file remains optional
seed data.

Each uncached batch consumes one Sales Navigator navigation reservation. The
final URL still has to retain every requested type and ID before results are
returned. Synthetic DOM fixtures describe the resolver algorithm only; replace
them with an anonymized capture when one is approved and available.
