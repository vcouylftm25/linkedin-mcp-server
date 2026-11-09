# LinkedIn Sales Navigator

Study and implementation notes for read-only Sales Navigator support. External product details were checked on 2026-11-01.

## Product surface and prospecting fit

Sales Navigator provides separate lead and account searches, saved lead/account lists, search filters, lead/account alerts, and InMail. Officially documented lead filters include title, seniority, function, current/past company, geography, industry, company headcount, and spotlights such as job changes in the last 90 days and posting in the last 30 days. Account filters include industry, headquarters, headcount and growth, revenue, department headcount, recent activity, buyer intent, and saved account lists. Search results are capped at 2,500 leads (100 pages) and 1,000 accounts (40 pages).

For Picanço's B2B work, the highest-value read path is: find banks, fintechs, payment providers and wealth managers by account attributes; then find executives and functional leaders in those target accounts by role, seniority, geography, and recent activity. Search results should preserve LinkedIn's own ordering, be capped to a small first-page sample, and include source URLs so a human can review each match in Sales Navigator. The search should not infer fit or enrich contact data beyond what LinkedIn renders.

## Risks and constraints

- LinkedIn's Help Center says third-party software that scrapes or automates its website is prohibited by its User Agreement and can result in account restriction. Read-only intent and slow pacing do not remove that policy risk. This project has chosen not to bypass controls or export large datasets; usage still remains at the account owner's discretion.
- UI markup and Sales Navigator URL/filter formats can change without notice. Prefer rendered `innerText`, generic profile/company anchors, official visible pagination, and conservative caps. Do not call private Voyager endpoints, inspect their payloads, defeat access controls, or imitate a human to evade detection.
- Sales Navigator access varies by subscription and trial. A failed route is not proof that the user lacks a subscription; distinguish login/verification, access denial, and an inconclusive page.
- LinkedIn documents a 2,500-lead / 1,000-account result ceiling. The MCP implementation uses a much smaller per-call ceiling and does not attempt to walk all pages.
- No invites, messages, InMail, posts, comments, saves, list creation, saved-search changes, or other account writes are in scope. InMail is documented only as a manual follow-up workflow; no compose or send tool is planned.

## Tool design

Implement a separate `sales_navigator` page reader and tool registration module. Keep the existing LinkedIn tools unchanged and surface a clear, typed access error when Sales Navigator is not available. The initial tool set is:

| Tool | Behavior | Limits |
| --- | --- | --- |
| `sales_nav_status` | Navigate to the Sales Navigator home surface and report whether the session reached a recognizable Sales Navigator route, plus the resulting URL and a short status explanation. A reached route does not establish an active subscription or trial. | One navigation; never inspect or change subscription settings. |
| `sales_nav_search_leads` | Read lead results from a user-provided Sales Navigator lead-search URL. This lets the user apply the current UI filters (title, seniority, current company, industry, geography, changed jobs, posted recently, etc.) without us guessing unstable filter IDs. | Accept only LinkedIn `/sales/search/people` URLs; up to three pages when requested; no automatic scrolling; at most 15 references per page. |
| `sales_nav_resolve_filters` | Resolve free-text labels through rendered Sales Navigator typeaheads. Read IDs from DOM attributes; when absent, select one exact suggestion and read its filter ID and text from the resulting Rest.li `query` URL. Ambiguous labels return candidates; no ID yields `unresolved`. | One page visit per uncached batch; cached in the prospecting SQLite state for 30 days by filter type, normalized label, and account locale. |
| `sales_nav_search_leads_by_filters` | Resolve free-text filter labels, including geography, through visible Sales Navigator typeaheads and a locale-aware 30-day cache, build the people search, then use the same checked reader. Geography accepts labels; raw IDs are not accepted. | Read-only; up to 45 results across at most three pages; ambiguity stops before search; missing Rest.li filter types, IDs, or requested keywords fail closed and invalidate affected cache entries. |
| `sales_nav_search_accounts` | Read account results from a user-provided Sales Navigator account-search URL. The UI URL carries the filters supported by the user's plan, including industry, headcount, headquarters, growth, and buyer intent. | Accept only LinkedIn `/sales/search/company` URLs; up to three pages when requested; no automatic scrolling; at most 15 references per page. |
| `sales_nav_list_leads` | Read saved leads from the user's view, if accessible, with source links. | Up to three pages when requested; no automatic scrolling; at most 15 references per page; no filtering that requires account writes. |

Results use `{url, sections: {search_results: raw_text}, references: {search_results: [...]}, pagination: {pages_fetched, next_offset, reason}}`. URL-based lead/account searches and saved-lead reads accept `max_pages` up to three; structured searches request enough pages for `max_results` (up to 45). References are only anchors recognized by the existing generic LinkedIn reference reader and are deduplicated; Sales Navigator-specific lead/company anchors may not be recognized. User-supplied URLs must be parsed and constrained to the exact LinkedIn host and the expected Sales Navigator paths before navigation. Existing generic tool timeout and browser serialization apply; pacing is a fixed conservative delay between navigations, not randomized evasion. Invite/message writes use their own caps; each Sales Navigator page visit, including a live filter-resolution batch, consumes the separate Sales Navigator navigation budget.

Saving a lead to a list and creating an InMail preview are excluded from the first release. Saving is a LinkedIn write, and an InMail draft would be easy to mistake for an approved send flow. The user's explicit restriction allows list saving only in mocks. A future feature would need a separate user-approved scope and stronger product policy review.

## Sources

- [Sales Navigator lead and account search filter definitions](https://www.linkedin.com/help/sales-navigator/answer/a1461507) — LinkedIn Help Center; checked 2026-11-01.
- [Search results limit in Sales Navigator](https://www.linkedin.com/help/linkedin/answer/a106030/search-results-limit-in-sales-navigator?lang=en) — LinkedIn Help Center; checked 2026-11-01.
- [Find your saved leads in Sales Navigator](https://www.linkedin.com/help/linkedin/answer/a108050/find-your-saved-leads-in-sales-navigator?lang=en) — LinkedIn Help Center; checked 2026-11-01.
- [Sales Navigator alerts](https://www.linkedin.com/help/sales-navigator/answer/a105133) — LinkedIn Help Center; checked 2026-11-01.
- [InMail Messages FAQ](https://www.linkedin.com/help/sales-navigator/answer/a554514) — LinkedIn Help Center; checked 2026-11-01.
- [Automated activity on LinkedIn](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin?lang=en) and [Prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software?lang=en) — LinkedIn Help Center; checked 2026-11-01.
- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement) — checked 2026-11-01.

## Status

Study and implementation are complete on `feat/sales-navigator`.

- Added `sales_nav_status`, `sales_nav_search_leads`, `sales_nav_resolve_filters`, `sales_nav_search_leads_by_filters`, `sales_nav_search_accounts`, and `sales_nav_list_leads`. URL searches remain unchanged. Structured searches resolve labels through the rendered typeahead and cache unique results beside prospecting state; `sales_nav_filters.json` is optional seed data. Resolution uses DOM attributes or, after selecting an exact visible suggestion, the resulting URL's Rest.li `query`; page response bodies are not inspected. The reader checks requested filter IDs and keywords against the final URL and invalidates mappings that LinkedIn drops.
- Added synthetic HTML browser tests and unit tests. Focused verification passed: 110 passed, 4 skipped; the 4 browser tests skipped because this environment has no Patchright Chromium installed. Ruff check/format, architecture generation check, and policy-trace check passed.
- The requested full-suite run completed with 8,499 passed, 921 skipped, 69 failed, and 2 errors. That run happened before the follow-up fixes to the facade and policy-trace contracts; the focused rerun after those fixes passed. The full run also showed unrelated process, browser lifecycle, and timing failures. It was not rerun after the focused fixes.
- The live `sales_nav_status` call could not acquire `~/.linkedin-mcp/profile.lock` because the sandbox denies writes outside the workspace. No LinkedIn page was opened and no account state was changed. The tool reports `subscription_status: "unknown"` because reaching a `/sales/` route alone cannot establish a trial or active subscription. Therefore no live lead/account search was attempted; verify status and both searches on a machine with permitted access to the existing profile.
- A browser DOM test fixture is included, but its four checks skipped locally due to the missing browser binary. Mock tests cover URL rejection, route redirects, the page bound, and the output cap.

No commit or push has been made yet.
