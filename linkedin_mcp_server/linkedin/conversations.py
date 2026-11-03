"""Messaging inbox, thread and conversation-search workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import asyncio
import json
import logging
import re

import anyio
import anyio.lowlevel
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInOperationError,
)
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.identifiers import (
    messaging_thread_url,
    normalize_person_identifier,
    normalize_thread_id,
    person_profile_url,
)
from linkedin_mcp_server.linkedin.link_metadata import (
    Reference,
    build_references,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.linkedin.text import (
    CONVERSATION_ACTIONS_EN_US,
    ConversationActionTextTable,
    strip_conversation_chrome,
    strip_linkedin_noise,
)

logger = logging.getLogger(__name__)

# The verb in a conversation-row label is locale-dependent. The account
# language is whatever the signed-in session stored; the browser context's
# en-US locale does not decide it. These are the shapes measured for the
# three languages this lookup accepts. Anything else is an unknown format
# and must be reported as such, not treated as a miss.
#
# The name is the only captured group. An optional group lets a trailing
# verb with no name stay a blank name ("Select conversation with ") rather
# than an unknown sentence. German puts the name between the noun and
# "auswählen"; English and Brazilian Portuguese are prefixes.
_ROW_LABEL_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("en", re.compile(r"^Select conversation with(?:\s+(.*))?$", re.IGNORECASE)),
    ("pt-BR", re.compile(r"^Selecionar conversa com(?:\s+(.*))?$", re.IGNORECASE)),
    ("de", re.compile(r"^Unterhaltung mit\s+(.+?)\s+auswählen$", re.IGNORECASE)),
    ("de", re.compile(r"^Konversation mit\s+(.+?)\s+auswählen$", re.IGNORECASE)),
    (
        "de",
        re.compile(r"^Konversation auswählen mit(?:\s+(.*))?$", re.IGNORECASE),
    ),
    (
        "de",
        re.compile(r"^Unterhaltung auswählen mit(?:\s+(.*))?$", re.IGNORECASE),
    ),
)


def normalize_participant_name(value: str) -> str:
    """Collapse whitespace and casefold, for full-string name equality.

    Both the profile-card heading and the row's participant name pass through
    this and nothing else. Equality is of the whole string, so "Ada Lovelace"
    does not select "Ada Lovelace-Group".
    """
    return re.sub(r"\s+", " ", value).strip().casefold()


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _name_from_label(value: str) -> tuple[str, str] | None:
    """The participant a known row label names, and that label's language."""
    text = _collapse_whitespace(value)
    if not text:
        return None
    for language, pattern in _ROW_LABEL_FORMATS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        captured = match.group(1)
        return _collapse_whitespace(captured or ""), language
    return None


def participant_name(*, visible: str, aria_label: str) -> tuple[str | None, str | None]:
    """The row's participant name, preferring its visible text over the label.

    Returns ``(name, language)``. ``language`` is a table key, ``"visible"``
    when the row shows a name that is not the aria-label sentence, or ``None``
    when the label is not a known format and there is no distinct visible
    name. A blank known label yields ``("", language)``. An unknown sentence
    yields ``(None, None)`` so the caller can say which label it saw.
    """
    visible_text = _collapse_whitespace(visible)
    label_text = _collapse_whitespace(aria_label)
    if visible_text and visible_text.casefold() != label_text.casefold():
        parsed = _name_from_label(visible_text)
        if parsed is not None:
            return parsed
        return visible_text, "visible"
    source = label_text or visible_text
    if not source:
        return "", None
    parsed = _name_from_label(source)
    if parsed is not None:
        return parsed
    return None, None


def strip_select_conversation_prefix(aria_label: str) -> str:
    """Drop a known selection verb from one conversation row's aria-label.

    An unrecognized label is returned unchanged, so a caller can still show
    the sentence it did not understand.
    """
    parsed = _name_from_label(aria_label)
    if parsed is None:
        return aria_label.strip()
    return parsed[0]


def _unrecognized_label_message(username: str, label: str) -> str:
    shown = label.strip() or "(empty label)"
    return (
        f"Could not read conversation rows for {username}: the label "
        f"{shown!r} is not a recognized format. Known formats are en "
        '("Select conversation with …"), pt-BR ("Selecionar conversa com …") '
        'and de ("Unterhaltung mit … auswählen", "Konversation mit … auswählen", '
        '"Unterhaltung auswählen mit …" or "Konversation auswählen mit …"). '
        "The browser locale is not consulted."
    )


# Click scans start here, never on bare `/messaging/`: that page opens a
# thread before the rows attach, and a click on the already-open row leaves the
# pathname unchanged, so it cannot be verified. Measured 2026-09-25, see
# docs/decisions/2026-09-25-fail-closed-thread-attribution.md.
_COMPOSE_URL = "https://www.linkedin.com/messaging/compose/"

_SCAN_STARTED_ON_THREAD = (
    " The scan began on a thread path. An unchanged pre-click thread ID was"
    " not accepted as evidence for a row."
)


@dataclass(frozen=True)
class _StoppedRow:
    """The row whose click did not open a different thread path.

    ``position`` counts considered labels, matching or not, so it orders a
    stop against an index gap from the same scan.
    """

    aria_label: str
    position: int


@dataclass(frozen=True)
class _IndexGap:
    """The first name-matching row that had no click target.

    ``preceded_by`` is how many rows had been attributed when it was skipped,
    which is where the index-eligible prefix ends.
    """

    aria_label: str
    position: int
    preceded_by: int


@dataclass(frozen=True)
class _ThreadRefScan:
    """One click scan: attributed rows plus why it may be incomplete.

    ``rows_available`` is false only when no row attached before the wait
    expired, in which case nothing was evaluated and ``start_thread_id`` is
    unknown rather than measured.
    """

    refs: list[Reference]
    stopped_at: _StoppedRow | None = None
    first_index_gap: _IndexGap | None = None
    start_thread_id: str | None = None
    rows_available: bool = True
    unrecognized_labels: tuple[str, ...] = ()


_BarrierReason = Literal["navigation", "missing_target", "name_rejected"]


@dataclass(frozen=True)
class _Barrier:
    reason: _BarrierReason
    verified_before: int


@dataclass(frozen=True)
class _ThreadResolution:
    """Thread URLs a username index may select, and where they stop.

    ``eligible_urls`` is the gap-free prefix of matching rows in scan order.
    With a barrier, a matching row at the next position could not be
    verified, so no later row may be numbered.
    """

    eligible_urls: list[str]
    barrier: _Barrier | None = None
    start_thread_id: str | None = None
    unrecognized_labels: tuple[str, ...] = ()


def _resolution_from_scan(display_name: str, scan: _ThreadRefScan) -> _ThreadResolution:
    """Convert a filtered scan into the prefix an index may select from.

    The prefix ends at the earliest of: a matching row without a click target,
    a click that did not open a different thread, or an attributed row that
    fails the exact display-name check below. Rows after that point are never
    renumbered into it, because a caller's ``index`` would then open a
    different conversation than the one at that position.
    """
    target_name = normalize_participant_name(display_name)
    gap, stop = scan.first_index_gap, scan.stopped_at
    reason: _BarrierReason | None = None
    candidates = len(scan.refs)
    if gap is not None and (stop is None or gap.position < stop.position):
        reason, candidates = "missing_target", gap.preceded_by
    elif stop is not None:
        reason = "navigation"

    eligible: list[str] = []
    for ref in scan.refs[:candidates]:
        # The browser already skipped other people. This is the same
        # normalization, so a spacing or case difference is not a new barrier.
        # A genuinely different string still ends the prefix.
        if normalize_participant_name(ref.get("text") or "") != target_name:
            return _ThreadResolution(
                eligible,
                _Barrier("name_rejected", len(eligible)),
                scan.start_thread_id,
                scan.unrecognized_labels,
            )
        eligible.append(f"https://www.linkedin.com{ref['url']}")
    barrier = None if reason is None else _Barrier(reason, len(eligible))
    return _ThreadResolution(
        eligible, barrier, scan.start_thread_id, scan.unrecognized_labels
    )


_UNVERIFIED_REASONS: dict[_BarrierReason, str] = {
    "navigation": (
        " that did not open a different thread within the poll budget. It may"
        " already be open, or the click may not have navigated in time."
    ),
    "missing_target": (
        " that has no click target, so later rows cannot be numbered safely."
    ),
    "name_rejected": (
        " that did not pass the exact display-name check, so later rows cannot"
        " be numbered safely."
    ),
}


def _unverified_index_message(
    index: int, username: str, resolution: _ThreadResolution
) -> str:
    barrier = resolution.barrier
    assert barrier is not None
    message = (
        f"Could not verify conversation index {index} for {username}: "
        f"{barrier.verified_before} conversation(s) were verified before a "
        f"matching row{_UNVERIFIED_REASONS[barrier.reason]}"
    )
    if resolution.start_thread_id is not None:
        message += _SCAN_STARTED_ON_THREAD
    return message + " Pass a known thread_id instead."


# The opened thread's options button, found by its accessible name: the
# `aria-label` or the screen-reader text inside it, whichever carries the
# table's prefix. The chrome table found that name in the page's innerText,
# so it is at least there as text. Every sidebar row holds one too, inside the `<li>` whose
# `label[aria-label]` names the row, so those are skipped, and more than one
# left is refused rather than guessed at. Marked for the click only when
# exactly one matches.
THREAD_OPTIONS_JS = r"""(opts) => {
    const {prefix, mark} = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    const inRow = (el) => {
        const row = el.closest('li');
        return !!(row && row.querySelector('label[aria-label]'));
    };
    const squash = (value) => (value || '').replace(/\s+/g, ' ').trim();
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const names = [];
    for (const button of main.querySelectorAll('button')) {
        if (!visible(button) || inRow(button)) continue;
        // The shortest match is the name alone, without the icon's text.
        const name = [
            button.getAttribute('aria-label'),
            ...[button, ...button.querySelectorAll('*')].map(el => el.textContent),
        ]
            .map(squash)
            .filter(value => value.startsWith(prefix))
            .sort((left, right) => left.length - right.length)[0];
        if (name) names.push([button, name]);
    }
    if (names.length === 1) names[0][0].setAttribute(mark, '');
    return {
        triggers: names.length,
        name: names.length === 1 ? names[0][1] : null,
    };
}"""

# The thread menu's items, found by structure rather than by LinkedIn's
# classes: the visible item-like elements with short text, innermost, that the
# options button's click revealed. They are told from what was already on the
# page by the ``seen`` attribute the survey puts on every visible item before
# the click, element by element, because a message in the thread can read the
# same as a menu item. Only those inside the button's own parent are its menu:
# measured on 2026-09-26 and 2026-10-15, the dropdown is `div[role=button]`
# items (`div[role=link]` for Manage settings) there. The scope never widens,
# so a control the click reveals elsewhere on the page, while the dropdown is
# still rendering or because LinkedIn moved it, is never one of its items.
# Shared by the two programs below, which start by unpacking ``after``,
# ``trigger`` and ``seen``.
_THREAD_MENU_PRELUDE_JS = r"""
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    const squash = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const itemSelector = '[role="button"], [role="menuitem"], [role="menuitemradio"], '
        + '[role="menuitemcheckbox"], [role="option"], button, a, li, [tabindex]';
    const found = [...document.querySelectorAll(itemSelector)].filter(el => {
        const text = squash(el.innerText);
        return visible(el) && text && text.length <= 80;
    });
    const items = found.filter(el => !found.some(other => other !== el && el.contains(other)));
    const button = document.querySelector('[' + trigger + ']');
    const revealed = after
        ? items.filter(el => el !== button && !el.hasAttribute(seen))
        : [];
    const menu = button?.parentElement || null;
    const menuItems = menu ? revealed.filter(el => menu.contains(el)) : [];
"""

# The menu's items whose whole text is the archive label. Marked for the
# click only when it is the one match.
THREAD_MENU_ITEMS_JS = (
    r"""(opts) => {
    const {archive, trigger, seen, mark} = opts;
    const after = true;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }"""
    + _THREAD_MENU_PRELUDE_JS
    + r"""
    const archiveItems = menuItems.filter(el => squash(el.innerText) === archive);
    if (archiveItems.length === 1) archiveItems[0].setAttribute(mark, '');
    return {archive: archiveItems.length};
}"""
)

# Before the click, marks everything already visible as seen. After it, evidence
# for a menu the read above could not use: the items beside the button, or
# when there are none, whatever the click revealed elsewhere, with the
# container holding them and the options button. Nothing is clicked.
THREAD_MENU_SURVEY_JS = (
    r"""(opts) => {
    const {after, trigger, seen} = opts;"""
    + _THREAD_MENU_PRELUDE_JS
    + r"""
    if (!after) {
        for (const old of document.querySelectorAll('[' + seen + ']')) {
            old.removeAttribute(seen);
        }
        for (const el of items) el.setAttribute(seen, '');
        return {seen: items.length};
    }
    const describe = (el) => {
        if (!el) return null;
        const role = el.getAttribute('role');
        const classes = [...el.classList].slice(0, 3).map(name => '.' + name).join('');
        return el.tagName.toLowerCase() + (role ? '[role=' + role + ']' : '')
            + (el.id ? '#' + el.id : '') + classes;
    };
    const beside = menuItems.length > 0;
    const added = beside ? menuItems : revealed;
    let container = beside ? menu : added[0]?.parentElement || null;
    while (container && !added.every(el => container.contains(el))) {
        container = container.parentElement;
    }
    const controls = button?.getAttribute('aria-controls');
    return {
        added: added.slice(0, 25).map(el => ({
            element: describe(el), text: squash(el.innerText),
        })),
        addedTotal: added.length,
        beside,
        container: describe(container),
        trigger: button ? {
            element: describe(button),
            expanded: button.getAttribute('aria-expanded'),
            haspopup: button.getAttribute('aria-haspopup'),
            controls: describe(controls ? document.getElementById(controls) : null),
        } : null,
    };
}"""
)

# The attributes that tie a click to the element the read chose.
_THREAD_OPTIONS_MARK = "data-mcp-thread-options"
_THREAD_ARCHIVE_MARK = "data-mcp-thread-archive"
_THREAD_SEEN_MARK = "data-mcp-thread-seen"

# How long the thread gets to load its folder and render its options button,
# and its menu to open.
_THREAD_READY_TIMEOUT = 10.0
_THREAD_MENU_TIMEOUT = 5.0
_THREAD_POLL = 0.25

# The folder a thread is in is read from LinkedIn's own messaging data, never
# from its menu: measured on 2026-10-15, an archived thread opened by its URL
# still offers Archive in its options menu. The client loads the open thread
# with the `messengerConversations` query, from the list when the thread is in
# the loaded window and by id otherwise, and each `Conversation` carries its
# `categories`: `["ARCHIVE"]` for the archived thread, `["INBOX",
# "PRIMARY_INBOX"]` for one in the inbox, and `["ARCHIVE", "INMAIL"]` for
# archived InMails in the same response.
_CONVERSATIONS_GRAPHQL_PATH = "/voyager/api/voyagerMessagingGraphQL/graphql"
_CONVERSATIONS_QUERY = "messengerConversations."
_CONVERSATION_TYPE = "com.linkedin.messenger.Conversation"
_ARCHIVE_CATEGORY = "ARCHIVE"
_INBOX_CATEGORY = "INBOX"

_ThreadArchiveState = Literal[
    # LinkedIn files it in the inbox and its menu offers Archive, marked for
    # the click.
    "archivable",
    # LinkedIn files it as archived. Its menu is not opened.
    "archived",
    # LinkedIn's data never named the thread's folder, or named both or
    # neither of the inbox and the archive.
    "no_folder",
    # No single options button could be told to be the thread's.
    "no_options",
    # The menu of an inbox thread showed no Archive item, or more than one.
    "no_item",
]


@dataclass(frozen=True)
class _ThreadArchiveRead:
    state: _ThreadArchiveState
    participant: str | None
    # For a failed read, what the page showed instead, in error-message words.
    evidence: str = ""


def is_conversations_payload_response(url: str) -> bool:
    """Whether ``url`` is a messaging query that loads conversations."""
    parsed = urlparse(url)
    return (
        parsed.netloc == "www.linkedin.com"
        and parsed.path == _CONVERSATIONS_GRAPHQL_PATH
        and any(
            query_id.startswith(_CONVERSATIONS_QUERY)
            for query_id in parse_qs(parsed.query).get("queryId", [])
        )
    )


def thread_categories(payload: Any, thread_key: str) -> list[str] | None:
    """The categories ``payload`` gives the conversation ``thread_key``.

    The conversation is the `Conversation` whose `backendUrn` names exactly that
    thread. None when the payload holds none, or holds it without a list of
    category strings.
    """
    urn = f"urn:li:messagingThread:{thread_key}"
    found: list[str] | None = None
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        categories = node.get("categories")
        if (
            node.get("_type") == _CONVERSATION_TYPE
            and node.get("backendUrn") == urn
            and isinstance(categories, list)
            and all(isinstance(category, str) for category in categories)
        ):
            found = list(categories)
        stack.extend(node.values())
    return found


class _ThreadFolderListener:
    """Read one thread's categories from the conversation payloads a load fetches.

    Installed before the navigation, because the open thread's conversation
    arrives with the page's first queries. Keeps the categories of the last
    payload that named the thread.
    """

    _READ_DRAIN_TIMEOUT = 2.0
    _CANCEL_DRAIN_TIMEOUT = 1.0

    def __init__(self, page: Any, thread_key: str):
        self._page = page
        self._thread_key = thread_key
        self._pending: list[asyncio.Task[None]] = []
        self.categories: list[str] | None = None

    def install(self) -> None:
        self._page.on("response", self._handle_response)

    def remove(self) -> None:
        try:
            # The registered method itself: Playwright matches listeners by
            # identity (see feed.py).
            self._page.remove_listener("response", self._handle_response)
        except Exception:
            logger.debug("Failed to remove thread folder listener", exc_info=True)

    def _handle_response(self, response: Any) -> None:
        if not is_conversations_payload_response(response.url):
            return

        async def _read() -> None:
            try:
                body = await response.body()
                payload = json.loads(body)
            except Exception:
                return
            categories = thread_categories(payload, self._thread_key)
            if categories is not None:
                self.categories = categories

        self._pending.append(asyncio.create_task(_read()))

    async def drain(self) -> None:
        """Bounded teardown for the response reads, as the permalink listener's."""
        pending = self._pending
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=self._READ_DRAIN_TIMEOUT)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait(pending, timeout=self._CANCEL_DRAIN_TIMEOUT)
                finally:
                    leftover = [task for task in pending if not task.done()]
                    for task in pending:
                        if task.done() and not task.cancelled():
                            task.exception()
                    if leftover:
                        logger.warning(
                            "Thread folder listener tasks did not drain after "
                            "cancel; leaking %d task(s)",
                            len(leftover),
                        )
            await anyio.lowlevel.checkpoint()
        self._pending = [task for task in pending if not task.done()]


def _folder_of(categories: list[str] | None) -> Literal["archived", "inbox"] | None:
    """The one folder ``categories`` put a thread in, or None."""
    if categories is None:
        return None
    archived = _ARCHIVE_CATEGORY in categories
    inbox = _INBOX_CATEGORY in categories
    if archived == inbox:
        return None
    return "archived" if archived else "inbox"


def _folder_finding(categories: list[str] | None) -> str:
    """Say which folder LinkedIn's data put a thread in, for an error message."""
    if categories is None:
        return "LinkedIn's messaging data never named its folder"
    return f"LinkedIn files it under {categories!r}"


def _menu_evidence(survey: dict[str, Any]) -> str:
    """Say what the options button opened, from a survey taken after its click."""
    trigger = survey.get("trigger") or {}
    parts = [
        f"The options button was {trigger.get('element')} with "
        f"aria-expanded={trigger.get('expanded')!r}, "
        f"aria-haspopup={trigger.get('haspopup')!r} and aria-controls naming "
        f"{trigger.get('controls')}."
    ]
    added = survey.get("added") or []
    if not added:
        parts.append("Its click revealed no visible item.")
    else:
        listed = ", ".join(f"{item['text']!r} ({item['element']})" for item in added)
        total = survey.get("addedTotal", len(added))
        where = (
            "Its click revealed"
            if survey.get("beside")
            else "Nothing appeared beside it; its click revealed elsewhere"
        )
        parts.append(
            f"{where} {total} visible item(s) in {survey.get('container')}: {listed}."
        )
    return " ".join(parts)


def _on_thread(url: str, thread_key: str) -> bool:
    """Whether ``url`` is the thread ``thread_key``, compared decoded."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/messaging/thread/([^/]+)/?", parsed.path)
    return (
        parsed.netloc == "www.linkedin.com"
        and match is not None
        and unquote(match.group(1)) == thread_key
    )


class ConversationReader:
    """Own every workflow whose subject is a LinkedIn messaging thread.

    The sidebar is the reason a thread id is not on the list. LinkedIn renders
    conversation rows with no anchor href, no thread-id attribute and no
    embedded URN, so a thread id can only be had by clicking a row. That click
    marks the row read. Listings therefore return the row text with
    ``thread_id`` null. Only the conversation being opened is clicked, and a
    username lookup clicks a row only after its visible name matches.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
        profile_page: ProfilePageReader,
        action_text: ConversationActionTextTable = CONVERSATION_ACTIONS_EN_US,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._profile_page = profile_page
        self._action_text = action_text

    @staticmethod
    def _single_section_result(
        url: str,
        section_name: str,
        text: str,
        references: list[Reference] | None = None,
    ) -> dict[str, Any]:
        """Build a standard single-section page response."""
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text:
            result["sections"][section_name] = text
            if references:
                result["references"] = {section_name: references}
        return result

    async def _wait_for_main_text(
        self,
        *,
        minimum_length: int = 100,
        timeout: int = 10000,
        log_context: str,
    ) -> None:
        """Wait for main content to populate enough text to read."""
        try:
            await self._session.page.wait_for_function(
                """({ minimumLength }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > minimumLength;
                }""",
                arg={"minimumLength": minimum_length},
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("%s content did not appear", log_context)

    async def _scroll_main_scrollable_region(
        self,
        *,
        position: Literal["top", "bottom"],
        attempts: int,
        pause_time: float = 0.5,
    ) -> None:
        """Scroll the largest scrollable region inside main when one exists."""
        for _ in range(attempts):
            await self._session.run_on_linkedin(
                """({ position }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;

                    const isScrollable = element => {
                        const style = window.getComputedStyle(element);
                        return (
                            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            element.scrollHeight > element.clientHeight + 20
                        );
                    };

                    const candidates = [main, ...main.querySelectorAll('*')].filter(isScrollable);
                    const target = candidates.sort(
                        (left, right) => right.scrollHeight - left.scrollHeight
                    )[0] || main;
                    target.scrollTop = position === 'top' ? 0 : target.scrollHeight;
                    return true;
                }""",
                {"position": position},
            )
            await self._session.delay(pause_time)

    async def _extract_conversation_thread_refs(
        self,
        limit: int | None,
        context: str,
        *,
        name_filter: str | None = None,
        scroll_attempts: int = 0,
    ) -> _ThreadRefScan:
        """Click each visible conversation item and capture the thread URL.

        Works for both the compose-page sidebar and the URL-driven
        search-results sidebar (`/messaging/?searchTerm=…`), which share the
        same DOM shape: each conversation row is an ``<li>`` containing a
        ``<label>`` with an ``aria-label`` attribute carrying the participant
        name. The caller navigates first; the scan never chooses its page.

        LinkedIn renders the sidebar with no ``<a href>`` tags, no
        ``data-thread-id`` attributes, and no embedded URNs — clicking each
        row and reading the SPA URL is the only reliable extraction path.
        Pass ``limit=None`` to consider every visible row.

        A row is attributed a thread id only when, after its click, the
        pathname carries a thread id different from the one it carried
        immediately before that click. The first click that does not produce
        one stops the scan and no later row is clicked, because that click may
        still land and would then be credited to whichever row came next.

        When ``name_filter`` is provided, every row is still read but only rows
        whose participant name equals it are clicked. The name is the row's
        visible text when that text is not the aria-label sentence, otherwise
        the name inside a known en, pt-BR or de label. Both sides use
        ``normalize_participant_name`` in Python before any click position is
        sent back to the browser.
        A label in no known format is not a miss: it is returned on
        ``unrecognized_labels`` and is not clicked. Clicking a row may mark it
        as read, so unrelated threads stay unclicked.

        ``scroll_attempts`` bottom scrolls run after the rows attach and before
        they are read, with the same heuristic as the inbox text page.
        """
        # The conversation list mounts after main text settles, so wait
        # explicitly for at least one label rather than relying on
        # _wait_for_main_text alone (which only checks chrome text). LinkedIn
        # routinely takes several seconds to hydrate the messaging sidebar
        # after a navigation; an empty sidebar (zero matches) returns on
        # timeout.
        #
        # Selector is structural (`main li label[aria-label]`) rather than
        # text-prefix-based (`aria-label^="Select conversation"`) so it
        # survives any LinkedIn locale — the verb in the aria-label is
        # locale-dependent, the attribute's presence inside a list-item label
        # is not.
        #
        # Wait on `state="attached"` instead of the default `visible`:
        # Ember-managed labels are reliably attached but Playwright's
        # visibility heuristic doesn't always consider them visible.
        try:
            await self._session.page.wait_for_selector(
                "main li label[aria-label]",
                state="attached",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            logger.debug(
                "conversation labels did not appear within 10s (context=%s)",
                context,
            )
            return _ThreadRefScan(refs=[], rows_available=False)

        if scroll_attempts > 0:
            await self._scroll_main_scrollable_region(
                position="bottom", attempts=scroll_attempts, pause_time=0.5
            )

        # Read the labels without clicking, then compare the row name and
        # profile heading through the same Python casefold normalization.
        rows: Any = await self._session.run_on_linkedin(
            """({ limit }) => {
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                const cap = limit == null ? labels.length : Math.min(labels.length, limit);
                const collapse = value => (value || '').replace(/\\s+/g, ' ').trim();
                return labels.slice(0, cap).map((label, position) => {
                    const row = label.closest('li') || label;
                    const headings = Array.from(row.querySelectorAll('h1, h2, h3'))
                        .map(node => collapse(node.innerText || node.textContent || ''))
                        .filter(Boolean);
                    const firstLine = ((row.innerText || '')
                        .split('\\n')
                        .map(line => line.trim())
                        .filter(Boolean)[0]) || '';
                    return {
                        position,
                        visible: headings.length === 1 ? headings[0] : firstLine,
                        ariaLabel: label.getAttribute('aria-label') || '',
                    };
                });
            }""",
            {"limit": limit},
        )
        selected_positions: list[int] = []
        participant_names: dict[int, str] = {}
        unrecognized_labels: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("position"), int):
                    continue
                name, _language = participant_name(
                    visible=str(row.get("visible") or ""),
                    aria_label=str(row.get("ariaLabel") or ""),
                )
                if name is None:
                    label = str(row.get("ariaLabel") or "")
                    if label:
                        unrecognized_labels.append(label)
                    continue
                if name_filter is not None and normalize_participant_name(
                    name
                ) != normalize_participant_name(name_filter):
                    continue
                position = row["position"]
                selected_positions.append(position)
                participant_names[position] = name

        # The Ember click handler lives on an inner div; neither the <li> nor
        # <label> triggers SPA navigation, so the generic class substring is
        # the only available target. Only Python-approved positions reach it.
        outcome: dict[str, Any] = await self._session.run_on_linkedin(
            """async ({ positions, participants, filtered }) => {
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                // Thread identity is the pathname's thread id and nothing
                // else: a query, hash or trailing-slash change on the same
                // thread is not a move, and a thread marker in a query is not
                // a thread.
                const idOf = () => {
                    const match = location.pathname.match(
                        /^\\/messaging\\/thread\\/([^/]+)\\/?$/
                    );
                    if (!match) return null;
                    try {
                        return decodeURIComponent(match[1]) === 'new' ? null : match[1];
                    } catch {
                        return null;
                    }
                };
                const outcome = {
                    rows: [],
                    stoppedAt: null,
                    firstIndexGap: null,
                    startThreadId: idOf(),
                };
                for (const position of positions) {
                    const label = labels[position];
                    if (!label) continue;
                    const ariaLabel = label.getAttribute('aria-label') || '';
                    const clickTarget = label.closest('li')
                        ?.querySelector('div[class*="listitem__link"]');
                    if (!clickTarget) {
                        // Skipped, not clicked. A listing loses only this
                        // row; a username index cannot count past it.
                        if (filtered && outcome.firstIndexGap === null) {
                            outcome.firstIndexGap = {
                                ariaLabel,
                                position,
                                precededBy: outcome.rows.length,
                            };
                        }
                        continue;
                    }
                    // Read fresh for every row: after an earlier row moved the
                    // page, the thread that was open at scan start is a real
                    // destination for its own row.
                    const before = idOf();
                    clickTarget.click();
                    // Poll for the SPA URL to settle on a different thread.
                    // The Ember click handler can take a moment to bind after
                    // the label mounts, and a fixed sleep races the click.
                    let after = null;
                    for (let waits = 0; waits < 12; waits++) {
                        await new Promise(r => setTimeout(r, 100));
                        const now = idOf();
                        if (now !== null && now !== before) {
                            after = now;
                            break;
                        }
                    }
                    // Stop, never continue: this click may still land, and a
                    // later row polled meanwhile would be handed its thread.
                    // Nor is the pre-click id credited to this row.
                    if (after === null) {
                        outcome.stoppedAt = { ariaLabel, position };
                        break;
                    }
                    outcome.rows.push({
                        ariaLabel,
                        threadId: after,
                        participant: participants[String(position)] || '',
                    });
                }
                return outcome;
            }""",
            {
                "positions": selected_positions,
                "participants": {
                    str(position): name
                    for position, name in participant_names.items()
                },
                "filtered": name_filter is not None,
            },
        )
        refs: list[Reference] = []
        for conv in outcome["rows"]:
            ref: Reference = {
                "kind": "conversation",
                "url": f"/messaging/thread/{conv['threadId']}/",
                "context": context,
            }
            if "participant" in conv and isinstance(conv.get("participant"), str):
                name = conv["participant"].strip()
            else:
                name = strip_select_conversation_prefix(conv.get("ariaLabel", ""))
            if name:
                ref["text"] = name
            refs.append(ref)
        stopped = outcome["stoppedAt"]
        gap = outcome["firstIndexGap"]
        return _ThreadRefScan(
            refs=refs,
            stopped_at=(
                None
                if stopped is None
                else _StoppedRow(stopped["ariaLabel"], stopped["position"])
            ),
            first_index_gap=(
                None
                if gap is None
                else _IndexGap(gap["ariaLabel"], gap["position"], gap["precededBy"])
            ),
            start_thread_id=outcome["startThreadId"],
            unrecognized_labels=tuple(unrecognized_labels),
        )

    async def _resolve_conversation_thread_urls(
        self, display_name: str
    ) -> _ThreadResolution:
        """Resolve the thread URLs a username ``index`` may select from.

        Scans the compose page's conversation list with click-to-capture
        because LinkedIn renders the messaging sidebar with no anchor hrefs, no
        data-thread attributes, and no embedded URNs — clicking each row and
        reading the resulting SPA URL is the only available extraction path.
        The compose page is the scan destination because the dated observation
        found it listed inbox rows without opening a thread; bare
        `/messaging/` opened one. Each row is still judged by its own click.
        The inbox list is
        used before `?searchTerm=` because LinkedIn's messaging search
        frequently returns "We didn't find anything" for a participant whose
        thread is plainly present in the inbox (issue #434). ``name_filter`` is
        passed to the enumerator so only matching rows are clicked; clicking a
        row may mark it read, so unrelated threads stay untouched.

        Matches by ``normalize_participant_name`` on the row's visible
        participant name, or on the name inside a known en, pt-BR or de
        aria-label when the visible text is that sentence. Duplicate threads
        for one person stay in sidebar order. A label in no known format is
        not "Could not find": the scan keeps the sentence and the opener
        reports the format. The browser's en-US locale is not an input.

        The search runs only when the inbox scan observed no matching row and
        hit no barrier, for instance when the rows never attached or the
        thread sits below the scrolled window. It never runs to extend or
        replace an inbox scan that stopped or has a gap, because a search
        result would then take the place of a position the inbox could not
        verify.

        Positions are those of the observed scan, which is not proven to order
        rows exactly as bare `/messaging/` does. Open a buried duplicate thread
        directly via ``thread_id`` (enumerate IDs with
        ``search_conversations``).
        """
        # Never bare `/messaging/` here: see _COMPOSE_URL.
        await self._navigator._navigate_to_page(_COMPOSE_URL)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        inbox = _resolution_from_scan(
            display_name,
            await self._extract_conversation_thread_refs(
                limit=None,
                context="inbox",
                name_filter=display_name,
                scroll_attempts=2,
            ),
        )
        # An incomplete inbox answer is returned as it is. Searching after a
        # barrier could substitute a same-name thread for the position the
        # inbox could not verify.
        if inbox.eligible_urls or inbox.barrier is not None:
            return inbox
        # An unknown label format is the answer. Searching would only hide it
        # behind "Could not find" when every row was unreadable.
        if inbox.unrecognized_labels:
            return inbox

        # Fallback: LinkedIn's messaging search. Unreliable (often returns
        # "We didn't find anything" even for present threads, see #434), so it
        # runs only when the inbox scan came up empty — e.g. a thread buried
        # below the scrolled inbox window.
        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(display_name)}"
        )
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        await self._wait_for_main_text(log_context="Messaging search results")
        return _resolution_from_scan(
            display_name,
            await self._extract_conversation_thread_refs(
                limit=None, context="search", name_filter=display_name
            ),
        )

    async def _open_conversation_by_username(
        self, linkedin_username: str, index: int = 0
    ) -> None:
        """Open the ``index``-th conversation thread for the named participant.

        ``index`` is 0-based over the verified prefix of matching rows from the
        compose-page scan, or from the search scan when the inbox scan found no
        matching row and no barrier. Positions follow the observed scan order.
        An index at or past an incomplete prefix is refused rather than served
        from a row that could not be verified.
        """
        if index < 0:
            raise LinkedInOperationError(f"index must be non-negative (got {index}).")

        linkedin_username = normalize_person_identifier(linkedin_username)
        profile_url = person_profile_url(linkedin_username, "/")
        await self._navigator._navigate_to_page(profile_url)
        await self._session.check_rate_limit()

        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        await self._session.dismiss_modal()
        display_name = await self._profile_page._read_profile_display_name()
        if not display_name:
            raise LinkedInOperationError(
                f"Could not resolve a display name for {linkedin_username}."
            )

        try:
            resolution = await self._resolve_conversation_thread_urls(display_name)
            thread_urls = resolution.eligible_urls
            if index >= len(thread_urls):
                # Not InvalidReferenceError: the username is valid and the page
                # could not be verified, which is worth an issue report.
                if resolution.barrier is not None:
                    raise LinkedInOperationError(
                        _unverified_index_message(index, linkedin_username, resolution)
                    )
                if resolution.unrecognized_labels and not thread_urls:
                    raise LinkedInOperationError(
                        _unrecognized_label_message(
                            linkedin_username, resolution.unrecognized_labels[0]
                        )
                    )
                if not thread_urls:
                    raise LinkedInOperationError(
                        f"Could not find a conversation for {linkedin_username}."
                    )
                raise LinkedInOperationError(
                    f"index {index} out of range: only {len(thread_urls)} "
                    f"thread(s) exist for {linkedin_username}."
                )

            await self._navigator._navigate_to_page(thread_urls[index])
        except PlaywrightTimeoutError as exc:
            raise LinkedInOperationError(
                "Messaging search results did not load in time."
            ) from exc

    async def _read_conversation_previews(self, limit: int) -> list[dict[str, Any]]:
        """Read up to ``limit`` row previews without selecting a row.

        Selecting a row marks that thread read, so this never clicks. The
        thread id is taken only from a thread link the row already has. The
        messaging list has none, and the preview then carries ``thread_id``
        null rather than a click-derived id. Seen receipts are not intercepted
        and no Voyager request is issued.
        """
        try:
            await self._session.page.wait_for_selector(
                "main li label[aria-label]",
                state="attached",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            return []

        rows = await self._session.run_on_linkedin(
            """({ limit }) => {
                // conversationPreview: list text only. This program does not click.
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                const cap = Math.min(labels.length, limit);
                const previews = [];
                for (let index = 0; index < cap; index++) {
                    const label = labels[index];
                    const row = label.closest('li') || label;
                    const headings = Array.from(row.querySelectorAll('h1, h2, h3'))
                        .map(node => (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ').trim())
                        .filter(Boolean);
                    const lines = (row.innerText || '')
                        .split('\\n')
                        .map(line => line.trim())
                        .filter(Boolean);
                    const visible = headings.length === 1 ? headings[0] : (lines[0] || '');
                    let threadId = null;
                    const anchor = row.querySelector('a[href*="/messaging/thread/"]');
                    if (anchor) {
                        try {
                            const url = new URL(
                                anchor.getAttribute('href') || anchor.href,
                                location.href
                            );
                            const match = url.pathname.match(
                                /^\\/messaging\\/thread\\/([^/]+)\\/?$/
                            );
                            if (match) {
                                const id = decodeURIComponent(match[1]);
                                if (id !== 'new') threadId = id;
                            }
                        } catch {
                            threadId = null;
                        }
                    }
                    previews.push({
                        visible,
                        ariaLabel: label.getAttribute('aria-label') || '',
                        text: (row.innerText || '').trim(),
                        threadId,
                    });
                }
                return previews;
            }""",
            {"limit": limit},
        )
        previews: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return previews
        for row in rows:
            if not isinstance(row, dict):
                continue
            name, _language = participant_name(
                visible=str(row.get("visible") or ""),
                aria_label=str(row.get("ariaLabel") or ""),
            )
            thread_id = row.get("threadId")
            previews.append(
                {
                    "participant": name or None,
                    "text": str(row.get("text") or ""),
                    "thread_id": thread_id
                    if isinstance(thread_id, str) and thread_id
                    else None,
                }
            )
        return previews

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox.

        Read from the compose page. Bare ``/messaging/`` opens a thread on its
        own, which marks that thread read, so the list is not loaded there.
        Rows are not clicked. ``previews`` carries each row's visible text and
        ``thread_id`` null unless the row already linked a thread. Open one
        thread with ``get_conversation`` when its body is needed; that open is
        the click, and it marks that thread read.
        """
        url = _COMPOSE_URL
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context="Messaging inbox")
        await self._session.dismiss_modal()

        scrolls = max(1, limit // 10)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=scrolls, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "inbox") if cleaned else []
        )
        previews = await self._read_conversation_previews(limit)

        result = self._single_section_result(
            url,
            "inbox",
            cleaned,
            references=references,
        )
        if previews:
            result["previews"] = previews
        return result

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username.

        ``index`` (0-based) selects which thread to open when a participant has
        multiple conversation threads — e.g. an organic 1-on-1 plus a separate
        InMail. Ignored when ``thread_id`` is provided. Use
        ``search_conversations`` to enumerate thread IDs first if disambiguation
        by index is impractical.

        Side effect when looked up by username: resolution click-visits only
        the row(s) whose visible name matches the profile card heading, to
        capture the thread id. Each of those visits may mark that thread read.
        Pass ``thread_id`` to open that one thread without scanning the list.
        """
        if not linkedin_username and not thread_id:
            raise InvalidReferenceError(
                "Provide at least one of linkedin_username or thread_id"
            )

        if thread_id:
            thread_id = normalize_thread_id(thread_id)
            await self._navigator._navigate_to_page(
                messaging_thread_url(thread_id, "/")
            )
        else:
            await self._open_conversation_by_username(
                linkedin_username or "", index=index
            )

        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context="Conversation")
        await self._session.dismiss_modal()
        await self._scroll_main_scrollable_region(
            position="top", attempts=3, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        # Conversation chrome first: a sidebar preview containing a generic
        # noise marker would otherwise truncate the page before the thread
        # markers are ever seen.
        cleaned = strip_conversation_chrome(raw) if raw else ""
        cleaned = strip_linkedin_noise(cleaned) if cleaned else ""
        references = (
            build_references(raw_result["references"], "conversation")
            if cleaned
            else []
        )
        return self._single_section_result(
            self._session.page.url,
            "conversation",
            cleaned,
            references=references,
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword.

        Uses LinkedIn's ``?searchTerm=`` URL parameter to drive the search
        rather than typing into the searchbox — the URL form is reliable
        regardless of how soon the messaging SPA mounts its searchbox role.

        ``limit`` caps how many result rows are returned as previews. Rows are
        not clicked, so a search does not mark them read. ``thread_id`` on a
        preview is null unless that row already linked a thread; open one
        result with ``get_conversation`` to read its body.
        """
        search_url = (
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(keywords)}"
        )
        await self._navigator._navigate_to_page(search_url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        await self._wait_for_main_text(log_context="Messaging search")

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "search_results")
            if cleaned
            else []
        )
        previews = await self._read_conversation_previews(limit)

        result = self._single_section_result(
            self._session.page.url,
            "search_results",
            cleaned,
            references=references,
        )
        if previews:
            result["previews"] = previews
        return result

    async def archive_conversation(self, thread_id: str) -> dict[str, Any]:
        """Archive one messaging thread from its own options menu.

        Opens ``/messaging/thread/<id>/``, which marks it read, and reads the
        thread's folder from the conversation data the page loads before
        touching anything. The menu cannot tell: an archived thread opened by
        its URL still offers Archive. A thread already archived is reported as
        such and its menu is never opened; only one in the inbox has Archive
        chosen. After the click the thread is loaded again and LinkedIn must
        file it as archived, because a click the page took is not yet a
        change LinkedIn kept.

        Returns:
            {url, thread_id, participant, status: "archived" | "already_archived"}
        """
        thread_key = unquote(normalize_thread_id(thread_id))
        url = messaging_thread_url(thread_key, "/")
        first = await self._read_thread_archive_state(url, thread_key)
        status = "already_archived"
        if first.state == "archivable":
            if not _on_thread(self._session.page.url, thread_key):
                await self._session.page.keyboard.press("Escape")
                raise LinkedInOperationError(
                    f"Thread {thread_key} was left for {self._session.page.url} "
                    "before Archive could be chosen. Nothing was clicked."
                )
            await self._session.page.locator(f"[{_THREAD_ARCHIVE_MARK}]").click(
                timeout=5000
            )
            await self._session.delay(NAV_DELAY)
            after = await self._read_thread_folder(url, thread_key)
            if _folder_of(after) != "archived":
                raise LinkedInOperationError(
                    f"Archive was chosen for thread {thread_key}, but after a "
                    f"reload {_folder_finding(after)}."
                )
            status = "archived"
        elif first.state != "archived":
            raise LinkedInOperationError(
                f"Thread {thread_key} was not archived: "
                f"{self._read_finding(first.state)}.{first.evidence}"
            )
        return {
            "url": url,
            "thread_id": thread_key,
            "participant": first.participant,
            "status": status,
        }

    def _read_finding(self, state: _ThreadArchiveState) -> str:
        """What a failed read found, in the words of the error it ends up in."""
        text = self._action_text
        if state == "no_options":
            return (
                "its options button could not be found: no single button named "
                f"{text.options_prefix!r} outside the conversation list"
            )
        if state == "no_item":
            return (
                f"its options menu did not offer exactly one {text.archive_item!r}. "
                "Nothing was clicked"
            )
        return "LinkedIn did not file it in exactly one of the inbox or the archive"

    async def _read_thread_folder(self, url: str, thread_key: str) -> list[str] | None:
        """Open the thread and read its categories from the conversation data.

        Raises when the navigation ends anywhere but that thread, after
        offering the page to the auth-barrier check: LinkedIn sends an id it
        does not know elsewhere, and that page belongs to another thread.
        Returns None when no payload named the thread before the deadline.
        """
        page = self._session.page
        listener = _ThreadFolderListener(page, thread_key)
        listener.install()
        try:
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            if not _on_thread(page.url, thread_key):
                await self._navigator._raise_if_auth_barrier(page.url)
                raise LinkedInOperationError(
                    f"Thread {thread_key} could not be opened: the navigation "
                    f"ended on {page.url}."
                )
            await self._session.dismiss_modal()
            deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
            while listener.categories is None:
                if self._session.monotonic() >= deadline:
                    break
                await self._session.delay(_THREAD_POLL)
            return listener.categories
        finally:
            listener.remove()
            await listener.drain()

    async def _read_thread_archive_state(
        self, url: str, thread_key: str
    ) -> _ThreadArchiveRead:
        """Read the thread's folder and, in the inbox, its menu's Archive item.

        Leaves the menu open with the Archive item marked when the state is
        ``archivable``, and closed otherwise. An archived thread's menu is
        never opened, and a thread whose options button cannot be found is
        still reported archived when LinkedIn files it so.
        """
        text = self._action_text
        page = self._session.page
        categories = await self._read_thread_folder(url, thread_key)
        folder = _folder_of(categories)
        if folder is None:
            return _ThreadArchiveRead(
                "no_folder", None, f" {_folder_finding(categories)}."
            )

        options: dict[str, Any] = {"triggers": 0, "name": None}
        deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
        while True:
            found = await page.evaluate(
                THREAD_OPTIONS_JS,
                {"prefix": text.options_prefix, "mark": _THREAD_OPTIONS_MARK},
            )
            if isinstance(found, dict):
                options = found
                if options["triggers"] == 1:
                    break
            if self._session.monotonic() >= deadline:
                if folder == "archived":
                    return _ThreadArchiveRead("archived", None)
                await record_page_trace(
                    page, "archive-conversation-no-options", extra=options
                )
                return _ThreadArchiveRead(
                    "no_options",
                    None,
                    f" {options['triggers']} options button(s) matched.",
                )
            await self._session.delay(_THREAD_POLL)
        name = options["name"] or ""
        participant = name[len(text.options_prefix) :].strip() or None
        if folder == "archived":
            return _ThreadArchiveRead("archived", participant)

        await page.evaluate(
            THREAD_MENU_SURVEY_JS,
            {
                "after": False,
                "trigger": _THREAD_OPTIONS_MARK,
                "seen": _THREAD_SEEN_MARK,
            },
        )
        await page.locator(f"main button[{_THREAD_OPTIONS_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _THREAD_MENU_TIMEOUT
        while True:
            menu = await page.evaluate(
                THREAD_MENU_ITEMS_JS,
                {
                    "archive": text.archive_item,
                    "trigger": _THREAD_OPTIONS_MARK,
                    "seen": _THREAD_SEEN_MARK,
                    "mark": _THREAD_ARCHIVE_MARK,
                },
            )
            if menu["archive"] == 1:
                return _ThreadArchiveRead("archivable", participant)
            if self._session.monotonic() >= deadline:
                break
            await self._session.delay(_THREAD_POLL)
        # The menu is still open: read it and take its picture before closing.
        survey = await page.evaluate(
            THREAD_MENU_SURVEY_JS,
            {"after": True, "trigger": _THREAD_OPTIONS_MARK, "seen": _THREAD_SEEN_MARK},
        )
        await record_page_trace(page, "archive-conversation-menu", extra=survey)
        await page.keyboard.press("Escape")
        return _ThreadArchiveRead(
            "no_item",
            participant,
            f" 1 options button matched. {_menu_evidence(survey)} A screenshot "
            "of the open menu is in the trace directory when tracing is on.",
        )
