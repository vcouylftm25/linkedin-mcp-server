"""Compose and publish a LinkedIn feed post through the share composer."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

COMPOSER_URL = "https://www.linkedin.com/feed/?shareActive=true"
MAX_POST_CHARS = 3000
_IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}
_COMPOSER_WAIT_SECONDS = 15.0
_PUBLISH_WAIT_SECONDS = 20.0
_POLL_SECONDS = 0.5

# LinkedIn's class names are build-generated and its labels are localised, so
# the composer is found by structure alone: the one visible <dialog> holding a
# contenteditable editor. Inside it the Post button is the only button with no
# aria-label (the icon buttons all carry one), and the dismiss button is the
# first button in document order. Anything other than exactly one match fails
# closed. Text and the DOM were both needed here: the editor's text is only in
# the DOM, and no URL or innerText identifies the Post button.
_COMPOSER_JS = """
(mode) => {
  const visible = (e) =>
    !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const deepAll = (selector) => {
    const found = [];
    const walk = (root) => {
      root.querySelectorAll(selector).forEach((e) => found.push(e));
      root.querySelectorAll('*').forEach((e) => {
        if (e.shadowRoot) walk(e.shadowRoot);
      });
    };
    walk(document);
    return found;
  };
  const buttonsIn = (root) => {
    const found = [];
    const walk = (node) => {
      node.querySelectorAll('button').forEach((b) => {
        if (visible(b)) found.push(b);
      });
      node.querySelectorAll('*').forEach((e) => {
        if (e.shadowRoot) walk(e.shadowRoot);
      });
    };
    walk(root);
    return found;
  };
  const dialogs = deepAll('dialog').filter(
    (d) => visible(d) && d.querySelector('[contenteditable="true"]')
  );
  const state = {
    dialogs: dialogs.length,
    editors: 0,
    text: '',
    postButtons: 0,
    postEnabled: false,
    imageInputs: 0,
    imageAttached: false,
    result: null,
  };
  if (dialogs.length !== 1) return state;
  const dialog = dialogs[0];
  const editors = Array.from(
    dialog.querySelectorAll('[contenteditable="true"]')
  ).filter(visible);
  state.editors = editors.length;
  const buttons = buttonsIn(dialog);
  const imageInputs = Array.from(dialog.querySelectorAll('input[type="file"]'))
    .filter((input) => (input.accept || '').includes('image'));
  state.imageInputs = imageInputs.length;
  state.imageAttached = imageInputs.some((input) => input.files.length > 0);
  const post = buttons.filter((b) => !b.hasAttribute('aria-label'));
  state.postButtons = post.length;
  if (editors.length !== 1) return state;
  const editor = editors[0];
  state.text = editor.innerText || '';
  state.postEnabled =
    post.length === 1 &&
    !post[0].disabled &&
    post[0].getAttribute('aria-disabled') !== 'true';
  if (mode === 'focus') {
    editor.focus();
    state.result = 'focused';
  } else if (mode === 'post') {
    if (state.postEnabled) {
      post[0].click();
      state.result = 'clicked';
    } else {
      state.result = 'unavailable';
    }
  } else if (mode === 'dismiss') {
    if (buttons.length) {
      buttons[0].click();
      state.result = 'clicked';
    } else {
      state.result = 'unavailable';
    }
  }
  return state;
}
"""


def refuse_an_invalid_post(text: str) -> dict[str, Any] | None:
    """Return a refusal for text that cannot be posted, else None."""
    if not isinstance(text, str) or not text.strip():
        return _result("invalid", "Post text is empty.")
    if len(text) > MAX_POST_CHARS:
        return _result(
            "invalid",
            f"Post is {len(text)} characters; LinkedIn allows {MAX_POST_CHARS}.",
        )
    if any((ord(c) < 32 and c not in "\r\n") or ord(c) == 127 for c in text):
        return _result("invalid", "Post text contains control characters.")
    return None


def refuse_an_invalid_image(image_path: str | None) -> dict[str, Any] | None:
    if image_path is None:
        return None
    if not isinstance(image_path, str) or not image_path.strip():
        return _result("invalid", "Image path is empty.")
    path = Path(image_path).expanduser()
    signatures = _IMAGE_SIGNATURES.get(path.suffix.lower())
    if signatures is None:
        return _result("invalid", "Image must be PNG, JPEG, GIF or WebP.")
    try:
        if not path.is_file():
            return _result("invalid", "Image does not exist or its type is invalid.")
        with path.open("rb") as image_file:
            header = image_file.read(12)
        if not any(header.startswith(signature) for signature in signatures) or (
            path.suffix.lower() == ".webp" and header[8:12] != b"WEBP"
        ):
            return _result("invalid", "Image does not exist or its type is invalid.")
    except OSError:
        return _result("invalid", "Image cannot be read.")
    return None


def _result(
    status: str,
    message: str,
    *,
    url: str = COMPOSER_URL,
    published: bool = False,
    retry_safe: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Build the structured response for the create_post tool.

    ``published`` is true only when a permalink is observed after the composer
    closes. ``retry_safe`` is false from the moment Post is clicked, because a
    caller that retries on ``unconfirmed`` could publish the same post twice.
    """
    return {
        "url": url,
        "status": status,
        "message": message,
        "published": published,
        "retry_safe": retry_safe,
        **extra,
    }


def _lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def _same_text(composer_text: str, wanted: str) -> bool:
    """Compare the non-empty lines, ignoring the blank lines the editor adds.

    The editor renders each paragraph break as several newlines in innerText,
    so the count of blank lines says nothing about what will be posted.
    """
    return _lines(composer_text) == _lines(wanted)


@dataclass
class _Progress:
    """Whether Post may have been clicked; once it has, cleanup must not run."""

    submitted: bool = False


class PostPublisher:
    """Own the share composer: type a post, verify it, publish on confirmation."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._page = session.page

    def _post_identity(self) -> dict[str, str]:
        """Only trust a permalink when navigation itself names the new post."""
        url = getattr(self._page, "url", "")
        match = re.fullmatch(
            r"https://www\.linkedin\.com/feed/update/(urn:li:(?:activity|share):\d+)/?",
            url,
        )
        return {"url": url, "urn": match.group(1)} if match else {}

    async def _composer(self, mode: str = "read") -> dict[str, Any]:
        state = await self._session.run_on_linkedin(_COMPOSER_JS, mode)
        return state if isinstance(state, dict) else {}

    async def _wait_for_composer(self) -> dict[str, Any] | None:
        deadline = self._session.monotonic() + _COMPOSER_WAIT_SECONDS
        while True:
            state = await self._composer()
            if state.get("editors") == 1:
                return state
            if self._session.monotonic() >= deadline:
                return None
            await self._session.delay(_POLL_SECONDS)

    async def _type(self, text: str) -> None:
        await self._composer("focus")
        lines = text.replace("\r\n", "\n").split("\n")
        for index, line in enumerate(lines):
            if line:
                await self._page.keyboard.insert_text(line)
            if index < len(lines) - 1:
                await self._page.keyboard.press("Enter")

    async def _empty_and_close(self, *, image_attached: bool = False) -> bool:
        """Empty the editor, then dismiss it; True when the composer is gone.

        An emptied composer closes without LinkedIn's save-as-draft question,
        which is a localised dialog this code would otherwise have to read.
        """
        try:
            if image_attached:
                await self._page.locator(
                    'dialog input[type="file"][accept*="image"]'
                ).set_input_files([])
            await self._composer("focus")
            await self._page.keyboard.press("ControlOrMeta+A")
            await self._page.keyboard.press("Backspace")
            await self._session.delay(0.3)
            await self._composer("dismiss")
            await self._session.delay(0.8)
            return (await self._composer()).get("dialogs") == 0
        except Exception:
            logger.debug("Could not close the post composer", exc_info=True)
            return False

    async def create_post(
        self,
        text: str,
        *,
        dry_run: bool = False,
        image_path: str | None = None,
        visibility: str = "current",
    ) -> dict[str, Any]:
        """Type *text* into the share composer; publish only when confirmed."""
        refusal = refuse_an_invalid_post(text)
        if refusal is not None:
            return refusal
        refusal = refuse_an_invalid_image(image_path)
        if refusal is not None:
            return refusal
        if visibility != "current":
            return _result(
                "invalid",
                "Audience selection cannot be verified without locale-dependent "
                "labels; use current to keep LinkedIn's selected audience.",
            )
        wanted = text.strip()

        await self._navigator._navigate_to_page(COMPOSER_URL)
        await self._session.check_rate_limit()
        opened = await self._wait_for_composer()
        if opened is None:
            return _result(
                "composer_unavailable",
                "LinkedIn's post composer did not open as a single editor.",
            )
        if opened.get("text", "").strip():
            # A saved draft is already in the editor. Typing would append to
            # it and the dry run would then close it, discarding it.
            return _result(
                "composer_occupied",
                "The composer already holds text, so nothing was typed. "
                "Empty or finish that draft on LinkedIn, then retry.",
            )

        progress = _Progress()
        try:
            result = await self._fill_and_submit(wanted, dry_run, image_path, progress)
        except BaseException as exc:
            if progress.submitted:
                # Post may already have been clicked, so the composer is left
                # alone and the caller is told not to retry blindly.
                if not isinstance(exc, Exception):
                    raise
                logger.warning(
                    "Post may have been submitted but its outcome could not be read",
                    exc_info=True,
                )
                return _result(
                    "unconfirmed",
                    "Post may have been clicked but its outcome could not be "
                    "read; check your profile activity before retrying.",
                    retry_safe=False,
                )
            await self._empty_and_close(image_attached=image_path is not None)
            raise

        if progress.submitted:
            # Never touch the composer once Post was clicked.
            return result
        if not await self._empty_and_close(image_attached=image_path is not None):
            logger.warning("The post composer could not be closed")
            result = {
                **result,
                "composer_closed": False,
                "message": f"{result['message']} The composer could not be "
                "cleared or closed; check LinkedIn for leftover text before "
                "retrying.",
            }
        return result

    async def _fill_and_submit(
        self, wanted: str, dry_run: bool, image_path: str | None, progress: _Progress
    ) -> dict[str, Any]:
        """Type and verify *wanted*, then click Post when confirmed.

        ``progress.submitted`` turns true before Post is clicked and goes back
        to false only when the page script proves that no click happened.
        """
        await self._type(wanted)
        if image_path is not None:
            before = await self._composer()
            if before.get("imageInputs") != 1:
                return _result(
                    "image_unavailable", "A unique image input was not found."
                )
            await self._page.locator(
                'dialog input[type="file"][accept*="image"]'
            ).set_input_files(image_path)
        await self._session.delay(0.8)
        state = await self._composer()
        details: dict[str, Any] = {
            "text_matches": _same_text(state.get("text", ""), wanted),
            "post_button_found": state.get("postButtons") == 1,
            "post_button_enabled": bool(state.get("postEnabled")),
            "image_attached": bool(state.get("imageAttached")),
            "visibility": "current_unverified",
            "preview_text": state.get("text", ""),
        }
        if not details["text_matches"]:
            return _result(
                "text_mismatch",
                "The composer text differs from the requested post; "
                "nothing was posted.",
                composer_text=state.get("text", "")[:500],
                **details,
            )
        if image_path is not None and not details["image_attached"]:
            return _result(
                "image_unavailable", "The image was not attached.", **details
            )
        if dry_run:
            return _result(
                "dry_run",
                "Composer filled and verified; nothing was posted. "
                "Call again with dry_run=False to publish.",
                **details,
            )
        if not details["post_button_enabled"]:
            return _result(
                "post_button_unavailable",
                "LinkedIn's Post button was not found exactly once or is "
                "disabled; nothing was posted.",
                **details,
            )

        progress.submitted = True
        clicked = await self._composer("post")
        if clicked.get("result") != "clicked":
            # Only an answer that says nothing was clicked clears the flag; an
            # empty or odd answer leaves the click possible.
            if clicked.get("result") == "unavailable":
                progress.submitted = False
                return _result(
                    "post_button_unavailable",
                    "The Post button could not be clicked; nothing was posted.",
                    **details,
                )
            return _result(
                "unconfirmed",
                "The Post click could not be confirmed; check your profile "
                "activity before retrying.",
                retry_safe=False,
                **details,
            )

        deadline = self._session.monotonic() + _PUBLISH_WAIT_SECONDS
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL_SECONDS)
            if (await self._composer()).get("dialogs") == 0:
                identity = self._post_identity()
                return _result(
                    "published" if identity else "submitted",
                    "The composer closed after Post was clicked. Check profile "
                    "activity to confirm publication when no permalink is available.",
                    published=bool(identity),
                    retry_safe=False,
                    url=identity.get("url", COMPOSER_URL),
                    **({"urn": identity["urn"]} if identity else {}),
                    **details,
                )
        return _result(
            "unconfirmed",
            "Post was clicked but the composer did not close; check your "
            "profile activity before retrying.",
            retry_safe=False,
            **details,
        )
