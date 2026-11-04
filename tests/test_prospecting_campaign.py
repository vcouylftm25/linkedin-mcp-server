"""Persistent campaign contracts; all actions use local fakes."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.linkedin.connection import (
    CampaignInviteError,
    campaign_invite_scope,
)
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.profile_lease import (
    ProfileLease,
    ProfileLeaseUnavailableError,
    get_profile_lease,
)
from linkedin_mcp_server.error_handler import CodedToolError
from linkedin_mcp_server.prospecting.domain import (
    IllegalTransition,
    InvalidLead,
    Lead,
    LeadState,
    PauseReason,
    validate_lead_identity,
)
from linkedin_mcp_server.prospecting.repository import (
    ApprovalRejected,
    ProspectingRepository,
)
from linkedin_mcp_server.prospecting.invite import perform_campaign_connect
from linkedin_mcp_server.prospecting.reconcile import (
    reconcile_unconfirmed,
    reconciled_state,
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
DEADLINE = (NOW + timedelta(hours=24)).isoformat()


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
    assert (
        validate_lead_identity("alice-smith", "https://www.linkedin.com/in/alice-smith")
        == "https://www.linkedin.com/in/alice-smith/"
    )
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
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
    result = runner.tick(
        campaign.id,
        "invite",
        lambda item: (
            calls.append(item) or ExecutionResult(LeadState.INVITED, "sent", True)
        ),
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


def _assert_account_pause(repo, campaign_id, reason):
    """The pause is one account row for 24 hours, not a campaign column."""
    status = repo.status(campaign_id, now=NOW)
    assert status["paused"] is True
    assert status["pause_reason"] == reason
    assert status["paused_until"] == DEADLINE
    later = repo.create_campaign(f"after-{reason}", now=NOW)
    later_status = repo.status(later.id, now=NOW)
    assert later_status["paused"] is True
    assert later_status["paused_until"] == DEADLINE
    with repo._open() as db:
        columns = db.execute("SELECT paused_until FROM campaigns").fetchall()
        account = db.execute(
            "SELECT paused_until, pause_reason FROM account_pause"
        ).fetchone()
    assert [row["paused_until"] for row in columns] == [None] * len(columns)
    assert account["paused_until"] == DEADLINE
    assert account["pause_reason"] == reason
    return later.id


def _record_throttle(status: int) -> None:
    from linkedin_mcp_server.core.throttle import watch_responses

    page = MagicMock()
    watch_responses(page)
    listeners = {call.args[0]: call.args[1] for call in page.on.call_args_list}

    class Request:
        """Weak-referenceable stand-in for a Playwright request."""

    request = Request()
    listeners["request"](request)
    listeners["response"](
        SimpleNamespace(
            status=status,
            url="https://www.linkedin.com/voyager/api/messaging/x",
            headers={},
            request=request,
        )
    )


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
    assert result.reason == PauseReason.WEEKLY_LIMIT.value
    assert result.paused_until == DEADLINE
    _assert_account_pause(repo, campaign.id, PauseReason.WEEKLY_LIMIT.value)


@pytest.mark.parametrize("status_code", [429, 999])
def test_throttle_evidence_pauses_the_account_for_24_hours(tmp_path, status_code):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)

    def executor(_item):
        _record_throttle(status_code)
        return ExecutionResult(LeadState.INVITED, "sent", True)

    result = runner.tick(campaign.id, "invite", executor)
    assert result.reason == PauseReason.RATE_LIMITED.value
    assert result.paused_until == DEADLINE
    later = _assert_account_pause(repo, campaign.id, PauseReason.RATE_LIMITED.value)
    repo.add_leads(
        later,
        [
            {
                "lead_vanity": "bob-jones",
                "profile_url": "https://www.linkedin.com/in/bob-jones/",
                "name": "Bob",
            }
        ],
        now=NOW,
    )
    repo.approve_batch(later, "invite", [{"lead_vanity": "bob-jones"}], now=NOW)
    blocked = runner.tick(
        later, "invite", lambda _item: pytest.fail("a paused account must not send")
    )
    assert blocked.attempted == 0
    assert blocked.reason == "paused"
    assert blocked.paused_until == DEADLINE


def test_a_served_response_does_not_pause_the_account(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)

    def executor(_item):
        _record_throttle(200)
        return ExecutionResult(LeadState.INVITED, "sent", True)

    result = runner.tick(campaign.id, "invite", executor)
    assert result.reason is None
    assert result.paused_until is None
    assert repo.status(campaign.id, now=NOW)["paused"] is False


def test_checkpoint_evidence_pauses_the_account_for_24_hours(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
    result = runner.tick(
        campaign.id,
        "invite",
        lambda _item: ExecutionResult(
            LeadState.INVITED,
            "sent",
            True,
            landing_url="https://www.linkedin.com/checkpoint/challenge/1",
        ),
    )
    assert result.reason == PauseReason.CHECKPOINT.value
    assert result.paused_until == DEADLINE
    _assert_account_pause(repo, campaign.id, PauseReason.CHECKPOINT.value)
    assert repo.get_lead("alice-smith", "invite").state is LeadState.INVITED


def test_checkpoint_exception_pauses_without_finishing_the_attempt(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)

    def executor(_item):
        raise AuthenticationError("LinkedIn checkpoint")

    result = runner.tick(campaign.id, "invite", executor)
    assert result.reason == PauseReason.CHECKPOINT.value
    assert result.paused_until == DEADLINE
    assert repo.get_lead("alice-smith", "invite").state is LeadState.INVITE_UNCONFIRMED
    _assert_account_pause(repo, campaign.id, PauseReason.CHECKPOINT.value)


def test_note_quota_pauses_the_account_for_24_hours(tmp_path):
    limits, repo, campaign = _campaign(tmp_path)
    _approved(repo, campaign.id)
    runner = CampaignRunner(repo, limits, auth_root=tmp_path, clock=lambda: NOW)
    result = runner.tick(
        campaign.id,
        "invite",
        lambda _item: ExecutionResult(None, "custom_note_limit_reached", False),
    )
    assert result.reason == PauseReason.NOTE_QUOTA.value
    assert result.paused_until == DEADLINE
    _assert_account_pause(repo, campaign.id, PauseReason.NOTE_QUOTA.value)


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


def test_tick_lease_can_be_reacquired_by_browser_in_same_process(tmp_path):
    from linkedin_mcp_server.prospecting.runner import exclusive_profile_lease

    lease = get_profile_lease(tmp_path / "profile")
    with exclusive_profile_lease(tmp_path):
        assert lease.held
        assert lease.try_acquire()
        lease.release()
        assert lease.held
    assert not lease.held


def test_approve_batch_only_selects_the_requested_kind(tmp_path):
    _limits, repo, campaign = _campaign(tmp_path)
    repo.add_leads(
        campaign.id,
        [
            {
                "lead_vanity": "same-person",
                "profile_url": "https://www.linkedin.com/in/same-person/",
                "name": "Same Person",
                "kind": "invite",
            },
            {
                "lead_vanity": "same-person",
                "profile_url": "https://www.linkedin.com/in/same-person/",
                "name": "Same Person",
                "kind": "message",
            },
        ],
        now=NOW,
    )
    with repo._open() as db:
        db.execute(
            "UPDATE leads SET state=? WHERE lead_vanity=? AND kind=?",
            (LeadState.MESSAGE_DRAFTED.value, "same-person", "message"),
        )
    repo.approve_batch(
        campaign.id,
        "message",
        [{"lead_vanity": "same-person", "text": "Hello"}],
        now=NOW,
    )
    assert repo.get_lead("same-person", "message").state is LeadState.MESSAGE_APPROVED
    assert repo.get_lead("same-person", "invite").state is LeadState.IMPORTED


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        ("pending", LeadState.INVITED),
        ("already_connected", LeadState.ACCEPTED),
        ("connectable", LeadState.INVITE_APPROVED),
        ("incoming_request", LeadState.INVITE_UNCONFIRMED),
        ("unavailable", LeadState.INVITE_UNCONFIRMED),
        (None, LeadState.INVITE_UNCONFIRMED),
    ],
)
def test_reconciliation_uses_only_positive_connection_evidence(reading, expected):
    assert reconciled_state(LeadState.INVITE_UNCONFIRMED, reading) is expected


def test_reconcile_unconfirmed_reads_each_lead_through_injected_reader():
    lead = Lead(
        lead_vanity="alice-smith",
        kind="invite",
        campaign_id="campaign",
        lead_urn=None,
        profile_url="https://www.linkedin.com/in/alice-smith/",
        name="Alice Smith",
        title=None,
        company=None,
        state=LeadState.INVITE_UNCONFIRMED,
        updated_at=NOW,
    )

    class Store:
        def __init__(self):
            self.leads = [lead]
            self.moves = []

        def unconfirmed_leads(self):
            return self.leads

        def move_lead(self, current, new_state):
            self.moves.append((current, new_state))

    store = Store()
    reads = []

    def reader(current):
        reads.append(current)
        return "connectable"

    changes = reconcile_unconfirmed(store, reader)
    assert reads == [lead]
    assert store.moves == [(lead, LeadState.INVITE_APPROVED)]
    assert [(change.before, change.after) for change in changes] == [
        (LeadState.INVITE_UNCONFIRMED, LeadState.INVITE_APPROVED)
    ]


async def test_campaign_connect_guard_requires_refusing_incoming_requests():
    called = []

    def connect(_username, **kwargs):
        called.append(kwargs)
        return "ok"

    assert perform_campaign_connect(connect, "alice-smith") == "ok"
    assert called[-1]["refuse_incoming"] is True
    with campaign_invite_scope(), pytest.raises(CampaignInviteError):
        await ConnectionActions.connect_with_person(
            object.__new__(ConnectionActions), "alice-smith"
        )


def test_campaign_connect_contract_passes_required_true_flag():
    received = {}

    def connect(username, *, note=None, dry_run=False, refuse_incoming):
        received.update(username=username, note=note, dry_run=dry_run)
        received["refuse_incoming"] = refuse_incoming
        return "connected"

    assert perform_campaign_connect(connect, "alice-smith") == "connected"
    assert received == {
        "username": "alice-smith",
        "note": None,
        "dry_run": False,
        "refuse_incoming": True,
    }


def test_configured_campaign_root_must_match_session_root(tmp_path):
    session = tmp_path / "auth"
    alternate = tmp_path / "elsewhere"
    with pytest.raises(ProspectingRootError):
        resolve_prospecting_root(session, str(alternate))
    assert resolve_prospecting_root(session, str(session)) == session.resolve()


def test_daemon_owner_rejects_a_second_prospecting_database(tmp_path):
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon.owner import create_owner_server
    from linkedin_mcp_server.prospecting.roots import ProspectingRootError

    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    config.server.prospecting_state_dir = str(tmp_path / "other-state")
    with pytest.raises(ProspectingRootError):
        create_owner_server(config=config, token="token", host="127.0.0.1", port=0)


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
