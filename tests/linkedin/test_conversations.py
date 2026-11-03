"""Tests for the messaging conversation owner.

The click-to-capture loop is the one thing in this module that changes state
on LinkedIn: selecting a row may mark the thread read. Every case that touches
the enumerator therefore asserts *which* rows were reached, not only what came
back. The JavaScript half of that ordering never executes under a mock and is
covered in ``tests/test_conversation_sidebar_dom.py`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import (
    ConversationReader,
    _IndexGap,
    _StoppedRow,
    _ThreadRefScan,
    _ThreadResolution,
    is_conversations_payload_response,
    normalize_participant_name,
    participant_name,
    strip_select_conversation_prefix,
    thread_categories,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession


async def _no_message_target() -> SimpleNamespace:
    """The top-card read the profile page reader borrows, unused here.

    Nothing in this module resolves a message target; only the display name is
    read off a profile page. Handing the reader a callable that fails loudly
    keeps that true rather than assumed.
    """
    raise AssertionError("the conversation reader never reads a message target")


def _reader(page: Any) -> ConversationReader:
    """Wire the conversation owner the way the facade does."""
    session = PageSession(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )


@pytest.fixture(autouse=True)
def session_boundaries():
    """Replace the shared page boundaries for every case in this module.

    ``delay`` included: the scroll loops pace themselves through it, and a real
    half-second per attempt would make the scroll-budget assertions below the
    slowest thing in the suite.
    """
    with (
        patch.object(
            PageSession, "check_rate_limit", new_callable=AsyncMock
        ) as rate_limit,
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock) as modal,
        patch.object(PageSession, "delay", new_callable=AsyncMock) as delay,
    ):
        yield SimpleNamespace(
            check_rate_limit=rate_limit, dismiss_modal=modal, delay=delay
        )


def _root(text: str, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"source": "root", "text": text, "references": references or []}


def _ref(url: str, text: str, context: str) -> Reference:
    return {"kind": "conversation", "url": url, "text": text, "context": context}


class TestStripSelectConversationPrefix:
    """Known row-label verbs, and the miss that must not invent a name.

    en, pt-BR and de are the table. Another language comes back whole so the
    lookup can quote it instead of reporting that the person was not found.
    """

    def test_strips_en_us_prefix(self):
        assert (
            strip_select_conversation_prefix("Select conversation with Jacki McMahan")
            == "Jacki McMahan"
        )

    def test_case_insensitive(self):
        assert (
            strip_select_conversation_prefix("select conversation with jacki mcmahan")
            == "jacki mcmahan"
        )

    def test_strips_pt_br_prefix(self):
        assert (
            strip_select_conversation_prefix("Selecionar conversa com Jacki McMahan")
            == "Jacki McMahan"
        )

    def test_strips_german_verb_final_and_verb_initial(self):
        assert (
            strip_select_conversation_prefix("Unterhaltung mit Jacki McMahan auswählen")
            == "Jacki McMahan"
        )
        assert (
            strip_select_conversation_prefix("Konversation auswählen mit Jacki McMahan")
            == "Jacki McMahan"
        )

    def test_returns_full_aria_when_no_known_format_matches(self):
        assert (
            strip_select_conversation_prefix(
                "Choisir la conversation avec Jacki McMahan"
            )
            == "Choisir la conversation avec Jacki McMahan"
        )

    def test_empty_input(self):
        assert strip_select_conversation_prefix("") == ""


class TestParticipantName:
    """One normalization, visible text first, full-string equality."""

    def test_visible_name_wins_over_a_shorter_pt_br_label(self):
        name, language = participant_name(
            visible="Ada Lovelace",
            aria_label="Selecionar conversa com Ada",
        )

        assert (name, language) == ("Ada Lovelace", "visible")
        assert normalize_participant_name(name or "") == normalize_participant_name(
            "  ada   lovelace "
        )
        assert normalize_participant_name("Ada Lovelace") != normalize_participant_name(
            "Ada Lovelace-Group"
        )

    def test_a_pt_br_label_is_the_name_when_it_is_also_the_visible_text(self):
        name, language = participant_name(
            visible="Selecionar conversa com Ada Lovelace",
            aria_label="Selecionar conversa com Ada Lovelace",
        )

        assert (name, language) == ("Ada Lovelace", "pt-BR")

    def test_an_unknown_label_is_not_a_name(self):
        assert participant_name(
            visible="Choisir la conversation avec Ada Lovelace",
            aria_label="Choisir la conversation avec Ada Lovelace",
        ) == (None, None)


def _row(name: str, thread_id: str) -> dict[str, str]:
    """One attributed row as the browser program returns it."""
    return {"ariaLabel": f"Select conversation with {name}", "threadId": thread_id}


def _outcome(
    *rows: dict[str, str],
    stopped_at: dict[str, Any] | None = None,
    first_index_gap: dict[str, Any] | None = None,
    start_thread_id: str | None = None,
) -> dict[str, Any]:
    """The row program's raw return value."""
    return {
        "rows": list(rows),
        "stoppedAt": stopped_at,
        "firstIndexGap": first_index_gap,
        "startThreadId": start_thread_id,
    }


def _scan(
    *refs: Reference,
    stopped_at: _StoppedRow | None = None,
    first_index_gap: _IndexGap | None = None,
    start_thread_id: str | None = None,
    rows_available: bool = True,
    unrecognized_labels: tuple[str, ...] = (),
) -> _ThreadRefScan:
    return _ThreadRefScan(
        refs=list(refs),
        stopped_at=stopped_at,
        first_index_gap=first_index_gap,
        start_thread_id=start_thread_id,
        rows_available=rows_available,
        unrecognized_labels=unrecognized_labels,
    )


def _stop(name: str, position: int) -> _StoppedRow:
    return _StoppedRow(f"Select conversation with {name}", position)


def _gap(name: str, position: int, preceded_by: int) -> _IndexGap:
    return _IndexGap(f"Select conversation with {name}", position, preceded_by)


def _thread(thread_id: str) -> str:
    return f"https://www.linkedin.com/messaging/thread/{thread_id}/"


COMPOSE = "https://www.linkedin.com/messaging/compose/"
SEARCH_JACKI = "https://www.linkedin.com/messaging/?searchTerm=Jacki+McMahan"
PROFILE_JACKI = "https://www.linkedin.com/in/jacki/"
JACKI = "Jacki McMahan"

STARTED_ON_THREAD = (
    " The scan began on a thread path. An unchanged pre-click thread ID was"
    " not accepted as evidence for a row."
)
REASON_NAVIGATION = (
    " that did not open a different thread within the poll budget. It may"
    " already be open, or the click may not have navigated in time."
)
REASON_MISSING_TARGET = (
    " that has no click target, so later rows cannot be numbered safely."
)
REASON_NAME_REJECTED = (
    " that did not pass the exact display-name check, so later rows cannot"
    " be numbered safely."
)


def _refusal(index: int, verified: int, reason: str, *, started: bool = False) -> str:
    return (
        f"Could not verify conversation index {index} for jacki: {verified} "
        f"conversation(s) were verified before a matching row{reason}"
        + (STARTED_ON_THREAD if started else "")
        + " Pass a known thread_id instead."
    )


class TestExtractConversationThreadRefs:
    async def test_only_python_matched_rows_reach_the_browser_click_loop(
        self, mock_page
    ):
        """Casefold equality runs before click positions are sent to the page."""
        reader = _reader(mock_page)
        calls: list[tuple[str, dict | None]] = []

        async def fake_evaluate(js: str, arg: dict | None = None) -> Any:
            calls.append((js, arg))
            if len(calls) == 1:
                return [
                    {
                        "position": 0,
                        "visible": "Jacki McMahan",
                        "ariaLabel": "Selecionar conversa com Jacki",
                    },
                    {
                        "position": 1,
                        "visible": "Someone Else",
                        "ariaLabel": "Select conversation with Someone Else",
                    },
                ]
            return _outcome()

        mock_page.evaluate = fake_evaluate

        await reader._extract_conversation_thread_refs(
            limit=50, context="inbox", name_filter="Jacki McMahan"
        )

        assert calls[0][1] == {"limit": 50}
        assert calls[1][1] == {
            "positions": [0],
            "participants": {"0": "Jacki McMahan"},
            "filtered": True,
        }
        assert "normalize_participant_name" not in calls[1][0]

    async def test_unicode_casefold_happens_before_a_row_is_clicked(self, mock_page):
        reader = _reader(mock_page)
        calls: list[tuple[str, dict | None]] = []

        async def evaluate(js: str, arg: dict | None = None) -> Any:
            calls.append((js, arg))
            if len(calls) == 1:
                return [
                    {
                        "position": 0,
                        "visible": "Straße",
                        "ariaLabel": "Selecionar conversa com Straße",
                    }
                ]
            return _outcome()

        mock_page.evaluate = evaluate

        await reader._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="STRASSE"
        )

        assert calls[1][1] == {
            "positions": [0],
            "participants": {"0": "Straße"},
            "filtered": True,
        }

    async def test_rows_that_never_attach_return_nothing_and_click_nothing(
        self, mock_page
    ):
        """A sidebar that never hydrates is unavailable, not an error.

        The early return is what keeps it from being a click loop over zero
        rows *after* a ten-second wait, so the evaluate assertion is the load
        bearing half. No scroll runs either: the scroll belongs to a list that
        attached. ``start_thread_id`` stays unknown, not measured.
        """
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no rows")
        )
        mock_page.evaluate = AsyncMock(return_value=_outcome())
        scroll = AsyncMock()

        with patch.object(reader, "_scroll_main_scrollable_region", scroll):
            outcome = await reader._extract_conversation_thread_refs(
                limit=None, context="inbox", scroll_attempts=2
            )

        assert outcome == _ThreadRefScan(refs=[], rows_available=False)
        mock_page.evaluate.assert_not_awaited()
        scroll.assert_not_awaited()

    async def test_the_row_wait_is_structural_attached_and_bounded(self, mock_page):
        """Selector, state and timeout, none of which the refs themselves show.

        The selector is structural rather than a locale-dependent aria-label
        prefix; ``attached`` rather than ``visible`` because Ember-managed
        labels are reliably attached and not reliably visible.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=_outcome())

        await reader._extract_conversation_thread_refs(limit=None, context="inbox")

        mock_page.wait_for_selector.assert_awaited_once_with(
            "main li label[aria-label]", state="attached", timeout=10000
        )

    async def test_the_requested_scrolls_run_after_the_wait_and_before_the_read(
        self, mock_page
    ):
        """Wait, then scroll, then read the rows the scroll loaded.

        The page grows a second row only after two bottom scrolls, so a scan
        that read first, or scrolled before the list attached and then read,
        returns one row instead of two.
        """
        reader = _reader(mock_page)
        order: list[str] = []
        scrolled = 0

        async def wait_for_selector(*_args: Any, **_kwargs: Any) -> None:
            order.append("wait")

        async def evaluate(js: str, arg: Any = None) -> Any:
            nonlocal scrolled
            if "isScrollable" in js:
                order.append("scroll")
                scrolled += 1
                return True
            if "positions" not in (arg or {}):
                order.append("rows")
                rows = [
                    {
                        "position": 0,
                        "visible": "Ada",
                        "ariaLabel": "Select conversation with Ada",
                    }
                ]
                if scrolled >= 2:
                    rows.append(
                        {
                            "position": 1,
                            "visible": "Grace",
                            "ariaLabel": "Select conversation with Grace",
                        }
                    )
                return rows
            order.append("click")
            return _outcome(_row("Ada", "2-ada"), _row("Grace", "2-grace"))

        mock_page.wait_for_selector = wait_for_selector
        mock_page.evaluate = evaluate

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox", scroll_attempts=2
        )

        assert order == ["wait", "scroll", "scroll", "rows", "click"]
        assert [ref["url"] for ref in outcome.refs] == [
            "/messaging/thread/2-ada/",
            "/messaging/thread/2-grace/",
        ]

    async def test_every_ref_carries_the_callers_context_label(self, mock_page):
        """The label is how a consumer tells an inbox row from a search hit."""
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=_outcome(_row("Jacki McMahan", "2-aaa"))
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="search_results"
        )

        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "context": "search_results",
                "text": "Jacki McMahan",
            }
        ]

    async def test_a_row_with_no_participant_name_omits_the_text_key(self, mock_page):
        """Omitted, not empty: a ref carrying ``text: ""`` claims a nameless
        participant, and the resolver's exact-equality match would accept it
        for a display name that stripped to nothing."""
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=_outcome(
                {"ariaLabel": "Select conversation with ", "threadId": "2-aaa"}
            )
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "context": "inbox",
            }
        ]

    async def test_the_stop_gap_and_start_thread_reach_python_unmodified(
        self, mock_page
    ):
        """Raw labels and considered-label positions, including an empty label.

        The stop carries the row's label as the browser read it, so an empty
        one must still be a stop: a truthiness check would drop it and the
        scan would read as complete.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=_outcome(
                _row("Tess", "2-t"),
                stopped_at={"ariaLabel": "", "position": 3},
                first_index_gap={
                    "ariaLabel": "Select conversation with Tess",
                    "position": 1,
                    "precededBy": 1,
                },
                start_thread_id="2-open",
            )
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="Tess"
        )

        assert outcome.stopped_at is not None
        assert outcome == _ThreadRefScan(
            refs=[_ref("/messaging/thread/2-t/", "Tess", "inbox")],
            stopped_at=_StoppedRow("", 3),
            first_index_gap=_gap("Tess", 1, 1),
            start_thread_id="2-open",
            rows_available=True,
        )

    async def test_an_attached_list_with_nothing_to_click_is_still_available(
        self, mock_page
    ):
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=_outcome())

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert outcome == _ThreadRefScan(refs=[], rows_available=True)


class TestResolveConversationThreadUrls:
    async def test_the_inbox_leg_scans_the_compose_page_without_a_text_wait(
        self, mock_page, session_boundaries
    ):
        """Compose, rate check, modal, then the filtered scan with two scrolls.

        Bare ``/messaging/`` opens a thread before its rows attach, and the
        open row's click cannot be verified, so the inbox leg never scans
        there. The two bottom scrolls now run inside the scan, after the rows
        attach; the resolver itself neither scrolls nor waits for text.
        """
        reader = _reader(mock_page)
        order: list[Any] = []
        nav = AsyncMock(side_effect=lambda url: order.append(("navigate", url)))
        session_boundaries.check_rate_limit.side_effect = lambda: order.append(
            "rate_limit"
        )
        session_boundaries.dismiss_modal.side_effect = lambda: order.append("modal")

        async def scan(**kwargs: Any) -> _ThreadRefScan:
            order.append(("scan", kwargs))
            return _scan(_ref("/messaging/thread/2-aaa/", JACKI, "inbox"))

        text_wait = AsyncMock()
        scroll = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", text_wait),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(reader, "_extract_conversation_thread_refs", scan),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert order == [
            ("navigate", COMPOSE),
            "rate_limit",
            "modal",
            (
                "scan",
                {
                    "limit": None,
                    "context": "inbox",
                    "name_filter": JACKI,
                    "scroll_attempts": 2,
                },
            ),
        ]
        text_wait.assert_not_awaited()
        scroll.assert_not_awaited()
        assert resolution == _ThreadResolution([_thread("2-aaa")])

    async def test_matches_keep_the_order_the_sidebar_gave_them(self, mock_page):
        """LinkedIn renders newest activity first and nothing here reorders it.

        ``index`` in the caller is positional against exactly this list, so a
        reversal silently reassigns every index a caller ever recorded.
        """
        reader = _reader(mock_page)
        scan = _scan(
            _ref("/messaging/thread/2-newest/", JACKI, "inbox"),
            _ref("/messaging/thread/2-middle/", JACKI, "inbox"),
            _ref("/messaging/thread/2-oldest/", JACKI, "inbox"),
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=scan,
            ),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert resolution.eligible_urls == [
            _thread("2-newest"),
            _thread("2-middle"),
            _thread("2-oldest"),
        ]
        assert resolution.barrier is None

    async def test_a_matching_inbox_never_reaches_the_search_fallback(self, mock_page):
        """The inbox comes first and the search only runs when it came up empty.

        LinkedIn's messaging search answers "We didn't find anything" for
        threads plainly present in the inbox (issue #434), so running it first
        would turn a resolvable participant into "Could not find a
        conversation" — and running it anyway would click a second set of rows.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        refs_mock = AsyncMock(
            return_value=_scan(_ref("/messaging/thread/2-aaa/", JACKI, "inbox"))
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [COMPOSE]
        assert refs_mock.await_count == 1
        assert resolution.eligible_urls == [_thread("2-aaa")]

    async def test_a_complete_empty_inbox_falls_back_to_the_messaging_search(
        self, mock_page
    ):
        """A thread buried below the scrolled inbox window is the last resort."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        refs_mock = AsyncMock(
            side_effect=[
                _scan(),
                _scan(_ref("/messaging/thread/2-ddd/", JACKI, "search")),
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            COMPOSE,
            SEARCH_JACKI,
        ]
        assert refs_mock.await_args_list[1].kwargs == {
            "limit": None,
            "context": "search",
            "name_filter": JACKI,
        }
        assert resolution == _ThreadResolution([_thread("2-ddd")])

    async def test_a_row_wait_timeout_is_complete_empty_and_may_search(self, mock_page):
        """The real scan helper: no rows attached on compose, so nothing was
        clicked and nothing was evaluated, and the search is allowed."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=[PlaywrightTimeoutError("no rows"), None]
        )
        mock_page.evaluate = AsyncMock(
            side_effect=[
                [
                    {
                        "position": 0,
                        "visible": JACKI,
                        "ariaLabel": f"Select conversation with {JACKI}",
                    }
                ],
                _outcome(_row(JACKI, "2-found")),
            ]
        )
        nav_mock = AsyncMock()
        scroll = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            COMPOSE,
            SEARCH_JACKI,
        ]
        assert mock_page.evaluate.await_count == 2
        scroll.assert_not_awaited()
        assert resolution == _ThreadResolution([_thread("2-found")])


@dataclass
class _Opened:
    navigations: list[str]
    scans: AsyncMock
    root: AsyncMock
    error: LinkedInOperationError | None


async def _open_by_username(
    mock_page: Any, scans: list[_ThreadRefScan], *, index: int = 0
) -> _Opened:
    """Drive the real opener and resolver over scripted scan outcomes."""
    reader = _reader(mock_page)
    nav = AsyncMock()
    scan_mock = AsyncMock(side_effect=scans)
    root = AsyncMock(return_value=_root("msg"))
    error: LinkedInOperationError | None = None
    with (
        patch.object(PageNavigator, "_navigate_to_page", nav),
        patch.object(
            ProfilePageReader,
            "_read_profile_display_name",
            new_callable=AsyncMock,
            return_value=JACKI,
        ),
        patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(reader, "_scroll_main_scrollable_region", new_callable=AsyncMock),
        patch.object(reader, "_extract_conversation_thread_refs", scan_mock),
        patch.object(PageContentReader, "_extract_root_content", root),
    ):
        try:
            await reader.get_conversation(linkedin_username="jacki", index=index)
        except LinkedInOperationError as exc:
            error = exc
    return _Opened(
        navigations=[call.args[0] for call in nav.await_args_list],
        scans=scan_mock,
        root=root,
        error=error,
    )


def _jacki(thread_id: str, context: str = "inbox") -> Reference:
    return _ref(f"/messaging/thread/{thread_id}/", JACKI, context)


class TestUsernameResolutionFailsClosed:
    """An index is served only from the gap-free verified prefix.

    Every case drives the real opener and resolver; only the scan outcome,
    the page boundaries and the transcript read are scripted. A refusal must
    happen before the thread navigation and before any transcript capture.
    """

    async def test_a_complete_empty_inbox_uses_the_search_result(self, mock_page):
        opened = await _open_by_username(
            mock_page, [_scan(), _scan(_jacki("2-t1", "search"))]
        )

        assert opened.error is None
        assert opened.navigations == [
            PROFILE_JACKI,
            COMPOSE,
            SEARCH_JACKI,
            _thread("2-t1"),
        ]
        opened.root.assert_awaited_once()

    async def test_a_stopped_inbox_is_refused_without_searching(self, mock_page):
        """The search would offer a same-name thread for the stopped position."""
        opened = await _open_by_username(
            mock_page,
            [
                _scan(stopped_at=_stop(JACKI, 0)),
                _scan(_jacki("2-other", "search")),
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAVIGATION)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        assert opened.scans.await_count == 1
        opened.root.assert_not_awaited()

    async def test_an_already_open_first_match_is_not_substituted(self, mock_page):
        """The first matching row was the thread open at scan start.

        Its click moved nothing, so it stops the scan. A search that then
        offered the participant's other thread would answer index 0 with the
        wrong conversation.
        """
        opened = await _open_by_username(
            mock_page,
            [
                _scan(stopped_at=_stop(JACKI, 0), start_thread_id="2-t0"),
                _scan(_jacki("2-t1", "search")),
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAVIGATION, started=True)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        opened.root.assert_not_awaited()

    async def test_a_verified_prefix_serves_earlier_indices_only(self, mock_page):
        scans = [_scan(_jacki("2-t0"), stopped_at=_stop(JACKI, 1))]

        first = await _open_by_username(mock_page, list(scans), index=0)
        second = await _open_by_username(mock_page, list(scans), index=1)

        assert first.error is None
        assert first.navigations == [PROFILE_JACKI, COMPOSE, _thread("2-t0")]
        assert str(second.error) == _refusal(1, 1, REASON_NAVIGATION)
        assert second.navigations == [PROFILE_JACKI, COMPOSE]
        second.root.assert_not_awaited()

    async def test_a_first_row_without_a_click_target_numbers_nothing(self, mock_page):
        """The later match is not compressed into index 0."""
        opened = await _open_by_username(
            mock_page, [_scan(_jacki("2-t1"), first_index_gap=_gap(JACKI, 0, 0))]
        )

        assert str(opened.error) == _refusal(0, 0, REASON_MISSING_TARGET)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        opened.root.assert_not_awaited()

    async def test_a_middle_row_without_a_click_target_ends_the_prefix(self, mock_page):
        scans = [
            _scan(
                _jacki("2-t0"),
                _jacki("2-t2"),
                first_index_gap=_gap(JACKI, 1, 1),
            )
        ]

        first = await _open_by_username(mock_page, list(scans), index=0)
        second = await _open_by_username(mock_page, list(scans), index=1)

        assert first.navigations[-1] == _thread("2-t0")
        assert str(second.error) == _refusal(1, 1, REASON_MISSING_TARGET)
        assert _thread("2-t2") not in second.navigations

    @pytest.mark.parametrize(
        ("scan", "reason"),
        [
            (
                _scan(
                    first_index_gap=_gap(JACKI, 0, 0),
                    stopped_at=_stop(JACKI, 1),
                ),
                REASON_MISSING_TARGET,
            ),
            (
                _scan(
                    stopped_at=_stop(JACKI, 0),
                    first_index_gap=_gap(JACKI, 1, 0),
                ),
                REASON_NAVIGATION,
            ),
        ],
        ids=["gap-then-stop", "stop-then-gap"],
    )
    async def test_the_earlier_barrier_names_the_reason(self, mock_page, scan, reason):
        opened = await _open_by_username(mock_page, [scan])

        assert str(opened.error) == _refusal(0, 0, reason)
        opened.root.assert_not_awaited()

    async def test_collapsed_whitespace_is_the_same_participant(self, mock_page):
        """Heading and row share one normalization.

        A double-spaced row the list rendered is the same name as the card
        heading. Treating the spaces as a different person used to refuse the
        index instead of opening the thread.
        """
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _ref("/messaging/thread/2-spaced/", "Jacki  McMahan", "inbox"),
                    _jacki("2-clean"),
                )
            ],
        )

        assert opened.error is None
        assert opened.navigations[-1] == _thread("2-spaced")

    async def test_a_different_name_is_a_barrier_not_a_skipped_row(self, mock_page):
        """A row the browser admitted that is not this person ends the prefix.

        Dropping it and counting on would make the real match after it
        index 0, which is not the thread at position 0.
        """
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _ref("/messaging/thread/2-other/", "Pat Jones", "inbox"),
                    _jacki("2-clean"),
                )
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAME_REJECTED)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]

    async def test_a_name_rejection_before_a_later_gap_wins(self, mock_page):
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _jacki("2-t0"),
                    _ref("/messaging/thread/2-spaced/", "Pat Jones", "inbox"),
                    _jacki("2-t2"),
                    first_index_gap=_gap(JACKI, 3, 3),
                )
            ],
            index=1,
        )

        assert str(opened.error) == _refusal(1, 1, REASON_NAME_REJECTED)

    async def test_a_name_rejection_after_an_earlier_gap_does_not_replace_it(
        self, mock_page
    ):
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _jacki("2-t0"),
                    _ref("/messaging/thread/2-spaced/", "Jacki  McMahan", "inbox"),
                    first_index_gap=_gap(JACKI, 1, 1),
                )
            ],
            index=1,
        )

        assert str(opened.error) == _refusal(1, 1, REASON_MISSING_TARGET)

    @pytest.mark.parametrize(
        ("search", "reason"),
        [
            (_scan(stopped_at=_stop(JACKI, 0)), REASON_NAVIGATION),
            (_scan(first_index_gap=_gap(JACKI, 0, 0)), REASON_MISSING_TARGET),
            (
                _scan(_ref("/messaging/thread/2-x/", "Pat Jones", "search")),
                REASON_NAME_REJECTED,
            ),
        ],
        ids=["stopped", "gapped", "rejected"],
    )
    async def test_the_search_leg_follows_the_same_prefix_rules(
        self, mock_page, search, reason
    ):
        opened = await _open_by_username(mock_page, [_scan(), search])

        assert str(opened.error) == _refusal(0, 0, reason)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE, SEARCH_JACKI]
        opened.root.assert_not_awaited()

    async def test_a_missing_target_refusal_reports_a_thread_start(self, mock_page):
        """The start sentence describes the scan, not a click on that row.

        The row had no click target and was never clicked.
        """
        opened = await _open_by_username(
            mock_page,
            [_scan(first_index_gap=_gap(JACKI, 0, 0), start_thread_id="2-open")],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_MISSING_TARGET, started=True)

    async def test_nothing_found_anywhere_is_still_could_not_find(self, mock_page):
        opened = await _open_by_username(mock_page, [_scan(), _scan()])

        assert str(opened.error) == "Could not find a conversation for jacki."
        assert opened.navigations == [PROFILE_JACKI, COMPOSE, SEARCH_JACKI]

    async def test_an_unknown_label_format_is_not_could_not_find(self, mock_page):
        """A Portuguese or other unread label must name itself.

        Treating every unrecognized row as a non-match is what produced
        "Could not find" for an inbox that was on screen the whole time.
        """
        label = "Choisir la conversation avec Jacki McMahan"
        opened = await _open_by_username(
            mock_page, [_scan(unrecognized_labels=(label,))]
        )

        assert opened.error is not None
        assert "Could not find" not in str(opened.error)
        assert label in str(opened.error)
        assert "pt-BR" in str(opened.error)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]

    async def test_a_complete_list_keeps_the_out_of_range_error(self, mock_page):
        opened = await _open_by_username(mock_page, [_scan(_jacki("2-t0"))], index=3)

        assert str(opened.error) == (
            "index 3 out of range: only 1 thread(s) exist for jacki."
        )

    @pytest.mark.parametrize(
        "scan",
        [
            _scan(stopped_at=_stop(JACKI, 0)),
            _scan(first_index_gap=_gap(JACKI, 0, 0)),
            _scan(_ref("/messaging/thread/2-x/", "Pat Jones", "inbox")),
        ],
        ids=["navigation", "missing_target", "name_rejected"],
    )
    async def test_a_refusal_is_the_base_operation_error_not_a_bad_reference(
        self, mock_page, scan
    ):
        """A valid username the page could not verify keeps its diagnostics.

        ``InvalidReferenceError`` is a subclass, so ``pytest.raises`` on the
        base class would accept it; the error mapper would then drop the issue
        diagnostics and present the refusal as the caller's mistake.
        """
        opened = await _open_by_username(mock_page, [scan])

        assert opened.error is not None
        assert type(opened.error) is LinkedInOperationError
        assert not isinstance(opened.error, InvalidReferenceError)


class TestOpenConversationByUsername:
    async def test_a_negative_index_is_refused_before_any_page_work(self, mock_page):
        """Argument checks run before navigation, so a bad call costs no hop.

        Reaching LinkedIn first would spend a profile navigation and a
        display-name read on a request that was never going to be served.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(LinkedInOperationError, match="non-negative"):
                await reader._open_conversation_by_username("jacki", index=-1)

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_a_traversal_username_is_refused_before_any_page_work(
        self, mock_page
    ):
        """Identifier validation sits on the same side of the navigation.

        The traversal value rather than a merely invalid one: a bare
        identifier builds the same URL whether or not it was normalized, so it
        is the only input whose result differs. This case is the conversation
        half of the table in
        ``tests/linkedin/test_facade_results.py::TestEveryNormalizedEntryPoint``, which the
        method left when the reader took it.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(InvalidReferenceError):
                await reader._open_conversation_by_username("../../feed")

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_a_profile_without_a_readable_name_is_refused(self, mock_page):
        """The display name is the only key the sidebar can be matched on."""
        reader = _reader(mock_page)
        resolve = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(reader, "_resolve_conversation_thread_urls", resolve),
        ):
            with pytest.raises(
                LinkedInOperationError, match="Could not resolve a display name"
            ):
                await reader._open_conversation_by_username("jacki")

        resolve.assert_not_awaited()


class TestGetInbox:
    async def test_returns_inbox_section(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Conversation A\nConversation B"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(),
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result == {
            "url": COMPOSE,
            "sections": {"inbox": "Conversation A\nConversation B"},
        }

    async def test_an_empty_inbox_omits_the_optional_keys(self, mock_page):
        """``sections`` stays but is empty, and ``references`` is absent.

        An empty-string section and an empty reference list are both
        indistinguishable from a read that worked and found nothing, which is
        the opposite of what an empty page means.
        """
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(""),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(),
            ),
        ):
            result = await reader.get_inbox(limit=5)

        assert result == {
            "url": COMPOSE,
            "sections": {},
        }

    async def test_the_list_is_read_on_compose_and_rows_are_not_clicked(
        self, mock_page, session_boundaries
    ):
        """Compose only. Bare ``/messaging/`` opens a thread and marks it read.

        The preview read is the last step, and it is not the click scan.
        """
        reader = _reader(mock_page)
        order: list[Any] = []
        nav = AsyncMock(side_effect=lambda url: order.append(("navigate", url)))
        session_boundaries.check_rate_limit.side_effect = lambda: order.append(
            "rate_limit"
        )
        session_boundaries.dismiss_modal.side_effect = lambda: order.append("modal")

        async def text_wait(**_kwargs: Any) -> None:
            order.append("text_wait")

        async def scroll(**kwargs: Any) -> None:
            order.append(("scroll", kwargs["attempts"]))

        async def root(_content: Any, _selectors: Any) -> dict[str, Any]:
            order.append("root")
            return _root("Conversation A")

        async def previews(limit: int) -> list[dict[str, Any]]:
            order.append(("previews", limit))
            return [
                {
                    "participant": "Ada Lovelace",
                    "text": "Ada Lovelace",
                    "thread_id": None,
                }
            ]

        click = AsyncMock(side_effect=AssertionError("inbox clicked a row"))

        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", text_wait),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(PageContentReader, "_extract_root_content", root),
            patch.object(reader, "_read_conversation_previews", previews),
            patch.object(reader, "_extract_conversation_thread_refs", click),
        ):
            result = await reader.get_inbox(limit=20)

        assert order == [
            ("navigate", COMPOSE),
            "rate_limit",
            "text_wait",
            "modal",
            ("scroll", 2),
            "root",
            ("previews", 20),
        ]
        click.assert_not_awaited()
        assert result["url"] == COMPOSE
        assert result["sections"] == {"inbox": "Conversation A"}
        assert result["previews"] == [
            {"participant": "Ada Lovelace", "text": "Ada Lovelace", "thread_id": None}
        ]

    async def test_anchor_refs_survive_and_previews_do_not_invent_a_thread_id(
        self, mock_page
    ):
        """A link already on the page is a reference. A row without one is null.

        Clicking the row to discover the id would mark it read, so the preview
        keeps thread_id null instead.
        """
        reader = _reader(mock_page)
        anchor = {
            "href": "https://www.linkedin.com/messaging/thread/2-anchor/",
            "text": "Anchor only",
        }
        click = AsyncMock(side_effect=AssertionError("inbox clicked a row"))
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Tony Chan", [anchor]),
            ),
            patch.object(
                reader,
                "_read_conversation_previews",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "participant": "Tony Chan",
                        "text": "Tony Chan\nhello",
                        "thread_id": None,
                    }
                ],
            ),
            patch.object(reader, "_extract_conversation_thread_refs", click),
        ):
            result = await reader.get_inbox(limit=10)

        click.assert_not_awaited()
        assert result["references"]["inbox"] == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-anchor/",
                "text": "Anchor only",
                "context": "inbox",
            }
        ]
        assert result["previews"][0]["thread_id"] is None
        assert "section_errors" not in result

    @pytest.mark.parametrize(
        ("limit", "attempts"),
        [(5, 1), (10, 1), (20, 2), (55, 5)],
    )
    async def test_the_scroll_budget_is_one_attempt_per_ten_requested_rows(
        self, mock_page, limit, attempts
    ):
        """A floor of one, and one more attempt per ten rows asked for.

        Without the floor a ``limit`` below ten scrolls not at all. The preview
        cap is the same limit, and the click scan is not how it is spent.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        previews = AsyncMock(return_value=[])
        click = AsyncMock(side_effect=AssertionError("inbox clicked a row"))
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("rows"),
            ),
            patch.object(reader, "_read_conversation_previews", previews),
            patch.object(reader, "_extract_conversation_thread_refs", click),
        ):
            await reader.get_inbox(limit=limit)

        scroll_mock.assert_awaited_once_with(
            position="bottom", attempts=attempts, pause_time=0.5
        )
        previews.assert_awaited_once_with(limit)
        click.assert_not_awaited()

    async def test_a_preview_without_a_link_keeps_thread_id_null(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Inbox text"),
            ),
            patch.object(
                reader,
                "_read_conversation_previews",
                new_callable=AsyncMock,
                return_value=[
                    {"participant": "Ada Lovelace", "text": "Ada", "thread_id": None}
                ],
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result["url"] == COMPOSE
        assert result["previews"] == [
            {"participant": "Ada Lovelace", "text": "Ada", "thread_id": None}
        ]
        assert "section_errors" not in result

    async def test_a_failed_navigation_propagates(self, mock_page):
        reader = _reader(mock_page)
        nav = AsyncMock(side_effect=LinkedInOperationError("navigation failed"))
        previews = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_read_conversation_previews", previews),
        ):
            with pytest.raises(LinkedInOperationError, match="navigation failed"):
                await reader.get_inbox(limit=10)

        assert [call.args[0] for call in nav.await_args_list] == [COMPOSE]
        previews.assert_not_awaited()


class TestGetConversation:
    @pytest.mark.parametrize(
        "thread_id",
        [
            "new",
            "/messaging/thread/new/",
            "https://www.linkedin.com/messaging/thread/new/?recipient=ACoAAB",
            "/messaging/thread/%6Eew/",
        ],
    )
    async def test_compose_thread_is_rejected_before_navigation(
        self, mock_page, thread_id
    ):
        reader = _reader(mock_page)
        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as nav:
            with pytest.raises(InvalidReferenceError, match="new-message composer"):
                await reader.get_conversation(thread_id=thread_id)

        nav.assert_not_awaited()

    async def test_returns_conversation_by_thread_id(self, mock_page):
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!\nHi there!"),
            ),
        ):
            result = await reader.get_conversation(thread_id="abc123")

        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )
        assert result["sections"]["conversation"] == "Hello!\nHi there!"

    async def test_a_thread_id_wins_over_a_username_and_its_index(self, mock_page):
        """``index`` is documented as ignored whenever ``thread_id`` is given.

        Honouring it alongside the id would send the direct path through the
        inbox enumeration it exists to skip, clicking rows and marking them
        read on a call that named its thread outright.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        open_by_username = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_open_conversation_by_username", open_by_username),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!"),
            ),
        ):
            await reader.get_conversation(
                linkedin_username="jacki-old", thread_id="abc123", index=1
            )

        open_by_username.assert_not_awaited()
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )

    async def test_strips_conversation_page_chrome(self, mock_page):
        """Conversation chrome is trimmed before the generic noise pass.

        The sidebar preview carries a generic noise marker, so the generic
        pass alone would truncate the page before the thread was ever read.
        """
        raw = (
            "Ada: Preview belonging to a different conversation\n"
            "Open the options list in your conversation with Ada and Grace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(raw),
            ),
        ):
            result = await reader.get_conversation(thread_id="abc123")

        assert result["sections"]["conversation"] == "Hello!"

    async def test_the_thread_scroll_walks_back_to_the_top(self, mock_page):
        """Three attempts, upward: a thread's oldest message is at the top.

        Scrolling to the bottom instead reaches the composer and loads nothing,
        so the read would return only the messages already on screen.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!"),
            ),
        ):
            await reader.get_conversation(thread_id="abc123")

        scroll_mock.assert_awaited_once_with(position="top", attempts=3, pause_time=0.5)

    async def test_no_identifier_is_refused_before_any_page_work(self, mock_page):
        """Neither argument means there is nothing to open, and nothing is."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(LinkedInOperationError, match="at least one of"):
                await reader.get_conversation()

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_an_unusable_thread_id_is_refused_before_any_page_work(
        self, mock_page
    ):
        """Normalization runs on the identifier before it becomes a URL."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(InvalidReferenceError):
                await reader.get_conversation(thread_id="../../feed")

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    @pytest.mark.parametrize(
        ("index", "expected"),
        [(0, "2-newer"), (1, "2-older")],
    )
    async def test_by_username_the_index_selects_positionally(
        self, mock_page, index, expected
    ):
        """0-based, against the resolver's order, and the default is the first."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=_ThreadResolution(
                    [
                        "https://www.linkedin.com/messaging/thread/2-newer/",
                        "https://www.linkedin.com/messaging/thread/2-older/",
                    ]
                ),
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("msg"),
            ),
        ):
            if index == 0:
                await reader.get_conversation(linkedin_username="jacki-old")
            else:
                await reader.get_conversation(
                    linkedin_username="jacki-old", index=index
                )

        target_calls = [
            call.args[0]
            for call in nav_mock.await_args_list
            if call.args and "/messaging/thread/" in call.args[0]
        ]
        assert target_calls == [
            f"https://www.linkedin.com/messaging/thread/{expected}/"
        ]

    async def test_by_username_an_index_past_the_end_raises(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=_ThreadResolution(
                    ["https://www.linkedin.com/messaging/thread/2-only/"]
                ),
            ),
        ):
            with pytest.raises(LinkedInOperationError, match="out of range"):
                await reader.get_conversation(linkedin_username="jacki-old", index=5)

    async def test_by_username_no_threads_raises_could_not_find(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=_ThreadResolution([]),
            ),
        ):
            with pytest.raises(
                LinkedInOperationError, match="Could not find a conversation"
            ):
                await reader.get_conversation(linkedin_username="jacki-old")


class TestSearchConversations:
    async def test_returns_search_results(self, mock_page):
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Result 1\nResult 2"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(),
            ),
        ):
            result = await reader.search_conversations("hello world")

        assert "Result 1" in result["sections"]["search_results"]
        # Search must be driven by the searchTerm URL parameter, not by typing
        # into the searchbox.
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/?searchTerm=hello+world"
        )

    async def test_search_returns_previews_without_clicking(self, mock_page):
        """Anchors stay. Previews do not come from a click, so thread_id is null."""
        reader = _reader(mock_page)
        anchors = [
            {
                "href": "https://www.linkedin.com/messaging/thread/2-anchor/",
                "text": "Anchor only",
            },
        ]
        click = AsyncMock(side_effect=AssertionError("search clicked a row"))
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Tony Chan", anchors),
            ),
            patch.object(
                reader,
                "_read_conversation_previews",
                new_callable=AsyncMock,
                return_value=[
                    {"participant": "Tony Chan", "text": "Tony Chan", "thread_id": None}
                ],
            ) as previews,
            patch.object(reader, "_extract_conversation_thread_refs", click),
        ):
            result = await reader.search_conversations("Jacki", limit=7)

        click.assert_not_awaited()
        previews.assert_awaited_once_with(7)
        assert result["previews"][0]["thread_id"] is None
        assert result["references"]["search_results"] == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-anchor/",
                "text": "Anchor only",
                "context": "search result",
            }
        ]

    async def test_an_empty_result_page_omits_the_optional_keys(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(""),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(),
            ),
        ):
            result = await reader.search_conversations("nothing matches")

        assert result == {"url": mock_page.url, "sections": {}}

    async def test_the_search_page_is_not_scrolled_before_it_is_read(self, mock_page):
        """The search sidebar renders its whole result set at once.

        The inbox and a thread both scroll first; this one deliberately does
        not, and a scroll added here would pace every search by half a second
        per attempt for nothing.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Result 1"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(),
            ),
        ):
            await reader.search_conversations("hello")

        scroll_mock.assert_not_awaited()

    async def test_a_search_without_rows_adds_no_diagnostic(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Some result text"),
            ),
            patch.object(
                reader,
                "_read_conversation_previews",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await reader.search_conversations("Jacki")

        assert result == {
            "url": mock_page.url,
            "sections": {"search_results": "Some result text"},
        }
        assert "section_errors" not in result


class TestScrollMainScrollableRegion:
    async def test_each_attempt_is_one_evaluation_paced_by_the_delay(
        self, mock_page, session_boundaries
    ):
        """The loop runs the program once per attempt and pauses after each.

        Dropping or zeroing the pause runs every scroll before the browser has
        appended anything, which reads as a sidebar with nothing more to load.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=True)

        await reader._scroll_main_scrollable_region(
            position="bottom", attempts=3, pause_time=0.25
        )

        assert mock_page.evaluate.await_count == 3
        assert [call.args[1] for call in mock_page.evaluate.await_args_list] == [
            {"position": "bottom"}
        ] * 3
        assert session_boundaries.delay.await_count == 3
        assert [call.args for call in session_boundaries.delay.await_args_list] == [
            (0.25,),
            (0.25,),
            (0.25,),
        ]

    async def test_a_zero_budget_evaluates_nothing(self, mock_page):
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=True)

        await reader._scroll_main_scrollable_region(position="top", attempts=0)

        mock_page.evaluate.assert_not_awaited()


class TestWaitForMainText:
    async def test_the_wait_carries_the_requested_minimum_and_timeout(self, mock_page):
        reader = _reader(mock_page)

        await reader._wait_for_main_text(
            minimum_length=250, timeout=1234, log_context="Messaging inbox"
        )

        call = mock_page.wait_for_function.await_args
        assert call.kwargs["arg"] == {"minimumLength": 250}
        assert call.kwargs["timeout"] == 1234

    async def test_a_page_that_never_fills_is_logged_and_not_raised(
        self, mock_page, caplog
    ):
        """A sparse page is still a page: the read continues on whatever is
        there rather than failing the whole call."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("never filled")
        )

        with caplog.at_level(
            "DEBUG", logger="linkedin_mcp_server.linkedin.conversations"
        ):
            await reader._wait_for_main_text(log_context="Messaging inbox")

        assert "Messaging inbox content did not appear" in caplog.text


_GRAPHQL = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"


def _conversation(thread_id: str, categories: Any) -> dict[str, Any]:
    return {
        "_type": "com.linkedin.messenger.Conversation",
        "backendUrn": f"urn:li:messagingThread:{thread_id}",
        "categories": categories,
    }


class TestThreadFolderPayload:
    """The archive workflow's folder read, from the conversations query's data."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (f"{_GRAPHQL}?queryId=messengerConversations.0d5e&variables=(x:1)", True),
            (f"{_GRAPHQL}?variables=(x:1)&queryId=messengerConversations.9501", True),
            (f"{_GRAPHQL}?queryId=messengerMessages.5846&variables=(x:1)", False),
            (f"{_GRAPHQL}?variables=(queryId:messengerConversations.1)", False),
            (
                "https://www.linkedin.com/voyager/api/graphql"
                "?queryId=messengerConversations.1",
                False,
            ),
            (
                "https://evil.example/voyager/api/voyagerMessagingGraphQL/graphql"
                "?queryId=messengerConversations.1",
                False,
            ),
        ],
    )
    def test_only_the_conversations_query_is_read(self, url, expected):
        assert is_conversations_payload_response(url) is expected

    def test_the_thread_is_found_by_its_backend_urn_anywhere_in_the_payload(self):
        payload = {
            "data": {
                "messengerConversationsBySyncToken": {
                    "elements": [
                        _conversation("2-other", ["ARCHIVE"]),
                        _conversation("2-abc==", ["INBOX", "PRIMARY_INBOX"]),
                    ]
                }
            }
        }

        assert thread_categories(payload, "2-abc==") == ["INBOX", "PRIMARY_INBOX"]

    @pytest.mark.parametrize(
        "conversation",
        [
            _conversation("2-other", ["ARCHIVE"]),
            _conversation("2-abc", None),
            _conversation("2-abc", ["ARCHIVE", 1]),
            {**_conversation("2-abc", ["ARCHIVE"]), "_type": "com.linkedin.Other"},
            # The message's reference to its thread is not the conversation.
            {
                "_type": "com.linkedin.messenger.Message",
                "backendConversationUrn": "urn:li:messagingThread:2-abc",
                "categories": ["ARCHIVE"],
            },
        ],
    )
    def test_anything_but_this_threads_conversation_says_nothing(self, conversation):
        assert thread_categories({"data": [conversation]}, "2-abc") is None
