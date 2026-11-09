"""Explicit write-attempt phase shared by action tools and page actions."""

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(slots=True)
class WriteAttempt:
    """Whether the action crossed the boundary where a write may dispatch."""

    attempted: bool = False

    def mark(self) -> None:
        self.attempted = True


_CURRENT: ContextVar[WriteAttempt | None] = ContextVar("write_attempt", default=None)


@contextmanager
def track_write_attempt(attempt: WriteAttempt) -> Iterator[None]:
    """Expose this call's marker to its action without changing facade APIs."""
    token: Token[WriteAttempt | None] = _CURRENT.set(attempt)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def mark_write_attempt() -> None:
    """Mark the current call immediately before an action can dispatch a write."""
    attempt = _CURRENT.get()
    if attempt is not None:
        attempt.mark()


def clear_write_attempt() -> None:
    """Clear a marker only after the action proves dispatch did not happen."""
    attempt = _CURRENT.get()
    if attempt is not None:
        attempt.attempted = False
