"""Identity a loaded person page states about itself."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from linkedin_mcp_server.linkedin.session import PageSession


class MessageTarget(Protocol):
    """The recipient identity a top-card compose action carries."""

    @property
    def profile_urn(self) -> str: ...


class MessageTargetResolution(Protocol):
    """One attempt at reading that identity off the current page."""

    @property
    def target(self) -> MessageTarget | None: ...


# The URN is read out of the same atomic top-card snapshot the messaging
# workflow resolves its recipient from, and that read still belongs to the
# facade until the message sender owns it. Taking it as a callable keeps the
# borrow explicit and one-directional: this module never learns what the
# facade is, and the day the sender owns the read, only the wiring moves.
ReadMessageTarget = Callable[[], Awaitable[MessageTargetResolution]]


class ProfilePageReader:
    """Read the profile URN and display name off the bound person page."""

    def __init__(
        self,
        session: PageSession,
        read_message_target: ReadMessageTarget,
    ):
        self._session = session
        self._read_message_target = read_message_target

    async def _extract_profile_urn(self) -> str | None:
        """Extract a profile URN only from one unambiguous top-card snapshot."""
        resolution = await self._read_message_target()
        return resolution.target.profile_urn if resolution.target else None

    async def _read_profile_display_name(self) -> str | None:
        """Read the profile card's single heading, the same one messaging uses.

        ``message_sender`` resolves a recipient from the top card's one visible
        heading (``_PROFILE_MESSAGE_TARGET_JS``). Conversation lookup compares
        that heading, not ``main h1``, whose innerText also carries a pronoun,
        a badge, or a second line. The section rules are the same; a compose
        link is not required, because this read only needs the name. Zero
        headings or more than one is no name, rather than a guess at the first
        line of the page.
        """
        display_name = await self._session.run_on_linkedin(
            """() => {
                // profileCardHeading: the top card's one heading, not main h1.
                const visible = element => {
                    const visibility = element && getComputedStyle(element).visibility;
                    return !!(
                        element &&
                        visibility !== 'hidden' &&
                        visibility !== 'collapse' &&
                        (element.offsetWidth || element.offsetHeight ||
                            element.getClientRects().length)
                    );
                };
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const main = document.querySelector('main');
                if (!main) return '';
                const pageProfile = /^\\/in\\/[^/]+\\/?$/.exec(window.location.pathname);
                const personPaths = section => Array.from(
                    section.querySelectorAll('a[href*="/in/"]')
                ).filter(visible).map(anchor => {
                    try {
                        const url = new URL(anchor.getAttribute('href'), window.location.href);
                        return url.hostname.toLowerCase().replace(/\\.$/, '') ===
                            window.location.hostname.toLowerCase().replace(/\\.$/, '') &&
                            /^\\/in\\/[^/]+\\/?$/.test(url.pathname)
                            ? url.pathname.replace(/\\/?$/, '/') : null;
                    } catch {
                        return null;
                    }
                }).filter(Boolean);
                const sections = Array.from(main.querySelectorAll('section')).filter(
                    element =>
                        visible(element) &&
                        !element.closest('aside') &&
                        !element.querySelector('section')
                );
                const currentPath = pageProfile
                    ? window.location.pathname.replace(/\\/?$/, '/') : null;
                const ownSections = currentPath
                    ? sections.filter(element => personPaths(element).includes(currentPath))
                    : [];
                if (ownSections.length > 1) return '';
                const section = currentPath
                    ? ownSections[0] || sections.find(element =>
                        !personPaths(element).some(path => path !== currentPath)
                    )
                    : sections[0];
                if (!section) return '';
                const headings = Array.from(section.querySelectorAll('h1, h2, h3'))
                    .filter(visible);
                if (headings.length !== 1) return '';
                return normalize(headings[0].innerText || headings[0].textContent || '');
            }"""
        )
        if not isinstance(display_name, str):
            return None
        display_name = display_name.strip()
        return display_name or None
