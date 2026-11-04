# Sales Navigator filter validation (10 minutes)

Read-only session with the banker. Do not save a search, add a lead, or change
any account setting. Compare only the URL produced by the UI and by the local
builder; do not open the generated link from this checklist.

## Before the session

- Confirm the banker has Sales Navigator open and can run a people search.
- Have this checklist and the builder output for each planned search at hand.
- Use a local, synthetic `sales_nav_search_leads_by_filters` call first. It
  returns an explicit catalog error when a requested label is not yet known.

## Ten-minute sequence

1. **0–2 min — CFO / São Paulo.** In the UI, select current title `CFO` and
   geography `São Paulo`; copy the resulting URL. Record the exact filter
   labels and the UI URL in the review notes, with account identifiers removed.
2. **2–4 min — Seniority / industry.** Select one seniority and one industry
   the banker commonly uses. Copy the URL and record each visible label.
3. **4–6 min — Headcount / recent signal.** Select one company size and, if
   available in this account, “changed jobs in the last 90 days” or a recent
   posting signal. Copy the URL. Mark controls unavailable on this account as
   unavailable, not as an empty result.
4. **6–8 min — Compare.** Decode each URL's `query` parameter once. Compare
   Rest.li filter `type`, `id`, `text`, and `selectionType` values. Compare the
   builder output to the UI URL after percent-decoding; ignore ordering and
   session-only parameters, but require every requested filter ID to remain.
   The read tool independently fails if a requested filter type or ID is
   absent from the landed URL.
5. **8–10 min — Review catalog candidates.** Add an entry only when the exact
   filter type and ID are confirmed by the captured UI URL. Record `source` as
   `measured:<date>` (account identifiers removed). Set `verified: true` only
   after the banker reruns the same search and the builder's URL lands on the
   same result count shown in the UI. A public source may be added as extra
   context but is not required: a URL captured from the live UI is the stronger
   evidence.

## Candidate comparison record

For each candidate, retain `filter_type`, `id`, `label_pt`, `label_en`, source
URL/title and access date, observed UI label, and the decoded URL fragment. Do
not infer IDs from nearby labels or from another account's URL. Geography stays
on the resolver hook until its typeahead IDs are measured; callers may supply a
raw geography ID for a one-off read-only lookup.
