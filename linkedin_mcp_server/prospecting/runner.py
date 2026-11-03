"""One-item-at-a-time campaign runner with an injected, test-only executor.

This phase deliberately supplies no LinkedIn executor. A caller must provide
one explicitly, which keeps this module usable with fakes and prevents an
accidental browser write from being wired into the campaign tools.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Protocol

from linkedin_mcp_server.error_handler import CodedToolError
from linkedin_mcp_server.profile_lease import (
    ProfileLeaseUnavailableError,
    _release_locked_fd,
    acquire_locked_fd,
)
from linkedin_mcp_server.prospecting.domain import LeadState, PauseReason, PendingSend
from linkedin_mcp_server.prospecting.repository import ProspectingRepository
from linkedin_mcp_server.prospecting_limits import ProspectingLimits


class Executor(Protocol):
    """An injected operation; this phase intentionally defines no real adapter."""

    def __call__(self, item: PendingSend) -> "ExecutionResult": ...


@dataclass(frozen=True)
class ExecutionResult:
    state: LeadState | None
    outcome: str
    submitted: bool
    pause_reason: PauseReason | None = None


@dataclass(frozen=True)
class TickResult:
    attempted: int
    remaining: bool
    paused_until: str | None = None
    reason: str | None = None


@contextmanager
def exclusive_profile_lease(auth_root: Path) -> Iterator[None]:
    """Hold the existing profile lock for the complete tick or refuse it."""
    try:
        fd = acquire_locked_fd(auth_root / "profile.lock", exclusive=True)
    except ProfileLeaseUnavailableError:
        raise
    if fd is None:
        raise ProfileLeaseUnavailableError(
            "An interactive session holds the LinkedIn profile; this tick was not run."
        )
    try:
        yield
    finally:
        _release_locked_fd(fd)


class CampaignRunner:
    def __init__(
        self,
        repository: ProspectingRepository,
        limits: ProspectingLimits,
        *,
        auth_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if repository.path.resolve() != limits.path.resolve():
            raise ValueError(
                "Campaigns and prospecting limits must share one SQLite file."
            )
        self.repository = repository
        self.limits = limits
        self.auth_root = auth_root.expanduser().resolve()
        self.clock = clock or (lambda: datetime.now().astimezone())

    def tick(
        self,
        campaign_id: str,
        action: str,
        executor: Executor,
        *,
        max_items: int = 1,
        after_begin: Callable[[PendingSend], None] | None = None,
    ) -> TickResult:
        """Run at most *max_items*, committing intent before each executor call.

        A process killed after ``begin_attempt`` leaves the lead unconfirmed
        and the quota reserved. The next tick sees no sendable row for it.
        ``after_begin`` is an injection seam used to prove this crash boundary.
        """
        if max_items < 1:
            raise ValueError("max_items must be at least one.")
        attempted = 0
        with exclusive_profile_lease(self.auth_root):
            campaign = self.repository.get_campaign(campaign_id)
            now = self.clock()
            if campaign.is_paused(now):
                return TickResult(0, True, campaign.paused_until.isoformat(), "paused")
            for _ in range(max_items):
                item = self.repository.next_sendable(campaign_id, action)
                if item is None:
                    break
                try:
                    self.repository.begin_attempt(self.limits, item, self.clock())
                except CodedToolError as exc:
                    reason = (
                        PauseReason.WEEKLY_LIMIT
                        if exc.structured_content
                        and exc.structured_content.get("window") == "week"
                        else None
                    )
                    if reason:
                        until = self.repository.pause(
                            campaign_id, reason, now=self.clock()
                        )
                        return TickResult(
                            attempted, True, until.isoformat(), reason.value
                        )
                    raise
                if after_begin is not None:
                    after_begin(item)
                attempted += 1
                result = executor(item)
                retain_reservation = result.submitted or result.outcome in {
                    "outcome_unknown",
                    "send_failed",
                }
                if retain_reservation:
                    paused_until = self.repository.finish_attempt(
                        self.limits,
                        item,
                        new_state=result.state,
                        outcome=result.outcome,
                        release_slots=False,
                        pause_reason=result.pause_reason,
                        now=self.clock(),
                    )
                else:
                    paused_until = self.repository.finish_attempt(
                        self.limits,
                        item,
                        new_state=result.state,
                        outcome=result.outcome,
                        release_slots=True,
                        pause_reason=result.pause_reason,
                        now=self.clock(),
                    )
                if result.pause_reason:
                    return TickResult(
                        attempted,
                        True,
                        paused_until.isoformat() if paused_until else None,
                        result.pause_reason.value,
                    )
        return TickResult(
            attempted,
            self.repository.next_sendable(campaign_id, action) is not None,
        )
