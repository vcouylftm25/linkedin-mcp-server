"""Persistent campaign contracts; all actions use local fakes."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from linkedin_mcp_server.profile_lease import (
    ProfileLease,
    ProfileLeaseUnavailableError,
)
from linkedin_mcp_server.error_handler import CodedToolError
from linkedin_mcp_server.prospecting.domain import (
    IllegalTransition,
    InvalidLead,
    LeadState,
    PauseReason,
    validate_lead_identity,
)
from linkedin_mcp_server.prospecting.repository import (
    ApprovalRejected,
    ProspectingRepository,
)
from linkedin_mcp_server.prospecting.runner import (
    CampaignRunner,
    ExecutionResult,
)
from linkedin_mcp_server.prospecting.roots import (
    ProspectingRootError,
    resolve_prospecting_root,
)
from linkedin_mcp_server.prospecting_limits import ProspectingLimits

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def _campaign(tmp_path):
    limits = ProspectingLimits(20, 40, str(tmp_path), weekly_invite_limit=100)
    repository = ProspectingRepository(limits.path)
    campaign = repository.create_campaign("Q4", now=NOW)
    repository.add_leads(
        campaign.id,
        [
            {
                "lead_vanity": "alice-smith",
                "profile_url": "https://www.linkedin.com/in/alice-smith/",
                "name": "Alice",
            }
        ],
        now=NOW,
    )
    return limits, repository, campaign


def _approved(repository, campaign_id):
    preview = repository.preview_batch(campaign_id, "invite")
    return repository.approve_batch(
        campaign_id,
        "invite",
        [{"lead_vanity": "alice-smith"}],
        preview["items_hash"],
        now=NOW,
    )


def test_domain_refuses_noncanonical_or_secret_bearing_profile_url():
    assert validate_lead_identity(
        "alice-smith", "https://www.linkedin.com/in/alice-smith"
    ) == "https://www.linkedin.com/in/alice-smith/"
    with pytest.raises(InvalidLead):
        validate_lead_identity(
            "alice-smith", "https://www.linkedin.com/in/alice-smith/?sessionId=secret"
        )


def test_incoming_request_is_terminal_and_never_approved(tmp_path):
    _limits, repo, campaign = _campaign(tmp_path)
    repo.transition_lead("alice-smith", "invite", LeadState.INCOMING_REQUEST, now=NOW)
    with pytest.raises(ApprovalRejected):
        repo.approve_batch(
            campaign.id, "invite", [{"lead_vanity": "alice-smith"}], now=NOW
        )
    with pytest.raises(IllegalTransition):
        repo.transition_lead(
            "alice-smith", "invite", LeadState.INVITE_APPROVED, now=NOW
        )


def test_global_lead_key_and_append_only_audit_are_enforced(tmp_path):
    _limits, repo, campaign = _campaign(tmp_path)
    second = repo.create_campaign("Q4 duplicate", now=NOW)
    duplicate = repo.add_leads(
        second.id,
        [
            {
                "lead_vanity": "alice-smith",
                "profile_url": "https://www.linkedin.com/in/alice-smith/",
                "name": "Alice Smith",
            }
        ],
        now=NOW,
    )
    assert duplicate["duplicates"] == ["alice-smith"]
    with repo._open() as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM events WHERE campaign_id=?", (campaign.id,))


def test_approval_hash_is_recomputed_and_consumed_per_item(tmp_path):
    _limits, repo, campaign = _campaign(tmp_path)
    preview = repo.preview_batch(campaign.id, "invite")
    with pytest.raises(ApprovalRejected):
        repo.approve_batch(
            campaign.id,
            "invite",
            [{"lead_vanity": "alice-smith", "note": "edited"}],
            preview["items_hash"],
            now=NOW,
        )
    approval = _approved(repo, campaign.id)
    assert approval["approval_token"].startswith("apv_")
    pending = repo.next_sendable(campaign.id, "invite")
    assert pending is not None
    assert pending.approval_token == approval["approval_token"]


def test_kill_after_intent_persisted_never_retries_the_send(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    script = """
import os, sys
from pathlib import Path
from linkedin_mcp_server.prospecting.repository import ProspectingRepository
from linkedin_mcp_server.prospecting.runner import CampaignRunner
from linkedin_mcp_server.prospecting_limits import ProspectingLimits
db_path, auth_root, campaign_id = sys.argv[1:]
limits = ProspectingLimits(20, 40, str(Path(db_path).parent), db_path=db_path)
repository = ProspectingRepository(db_path)
runner = CampaignRunner(repository, limits, auth_root=Path(auth_root))
runner.tick(campaign_id, "invite", lambda _: os._exit(18), after_begin=lambda _: os._exit(17))
"""
    killed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(limits.path),
            str(tmp_path),
            campaign.id,
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert killed.returncode == 17
    calls = []
    result = runner.tick(
        campaign.id,
        "invite",
        lambda item: calls.append(item)
        or ExecutionResult(LeadState.INVITED, "sent", True),
    )
    assert calls == []
    assert result.attempted == 0
    assert repo.get_lead("alice-smith", "invite").state is LeadState.INVITE_UNCONFIRMED
    assert limits.report()["invites"]["used"] == 1
    assert limits.report()["navigations"]["used"] == 1
    limits.limits["invite"] = 1
    with limits._open() as db:
        db.execute("UPDATE writes SET day='2020-01-01' WHERE kind='invite'")
    with pytest.raises(CodedToolError):
        limits.reserve("invite")


def test_weekly_limit_pauses_campaign_for_24_hours(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    limits.limits["invite"] = 0
    _approved(repo, campaign.id)
    for _ in range(100):
        limits.reserve("invite")
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
    result = runner.tick(
        campaign.id, "invite", lambda _item: pytest.fail("limit must block executor")
    )
    status = repo.status(campaign.id, now=NOW)
    assert result.reason == PauseReason.WEEKLY_LIMIT.value
    assert status["paused"] is True


def test_unknown_and_failed_outcomes_cannot_be_released(tmp_path):
    limits = ProspectingLimits(0, 0, str(tmp_path), weekly_invite_limit=10)
    for outcome in ("outcome_unknown", "send_failed"):
        token = limits.reserve("invite")
        limits.note_outcome(token, outcome)
        limits.release(token)
    assert limits.report()["weekly_invites"]["used"] == 2


def test_non_submission_releases_write_slot_but_keeps_profile_visit(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
    runner.tick(
        campaign.id,
        "invite",
        lambda _item: ExecutionResult(
            LeadState.ALREADY_CONNECTED, "not_submitted", False
        ),
    )
    counts = limits.report()
    assert counts["invites"]["used"] == 0
    assert counts["navigations"]["used"] == 1


def test_profile_lease_refuses_tick_during_interactive_session(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    lease = ProfileLease(tmp_path)
    assert lease.try_acquire()
    try:
        runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
        with pytest.raises(
            ProfileLeaseUnavailableError, match="interactive session holds"
        ):
            runner.tick(
                campaign.id,
                "invite",
                lambda _item: pytest.fail("must not run"),
            )
    finally:
        lease.release()


def test_configured_campaign_root_must_match_session_root(tmp_path):
    session = tmp_path / "auth"
    alternate = tmp_path / "elsewhere"
    with pytest.raises(ProspectingRootError):
        resolve_prospecting_root(session, str(alternate))
    assert resolve_prospecting_root(session, str(session)) == session.resolve()


async def test_campaign_tools_are_registered_without_any_executor(tmp_path):
    from fastmcp import FastMCP

    from linkedin_mcp_server.tools.prospecting import register_prospecting_tools

    limits = ProspectingLimits(20, 40, str(tmp_path))
    server = FastMCP("campaign-tests")
    register_prospecting_tools(server, limits=limits)
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "create_campaign",
        "add_leads",
        "preview_batch",
        "approve_batch",
        "campaign_status",
        "export_campaign",
    } <= names
