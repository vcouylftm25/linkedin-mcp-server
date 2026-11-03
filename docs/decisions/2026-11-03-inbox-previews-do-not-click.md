# Inbox and search return previews without clicking rows

- Date: 2026-11-03
- Supersedes: the listing rows of [2026-09-25](2026-09-25-fail-closed-thread-attribution.md)

## Decision

`get_inbox` and `search_conversations` read the conversation list and do not
click rows. Clicking a row marks that thread read. The list text is the
preview. `thread_id` is null unless the row already has a thread link. The
messaging list does not. Opening one thread is `get_conversation`, and that
open is the click.

`get_inbox` reads the compose page. Bare `/messaging/` opens a thread on its
own.

Username lookup still click-visits only rows whose visible participant name
matches the profile card heading, after one normalization (collapsed
whitespace, casefold, full string). Known label shapes are en, pt-BR and de.
An unrecognized label is an error that quotes the label. The browser locale
is not an input. The click-attribution rule in the 2026-09-25 record still
decides each of those clicks.

Seen receipts are not intercepted. Voyager is not called.
