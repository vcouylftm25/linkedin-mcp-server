"""Daily write caps have a persistent, browser-free contract."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.prospecting_limits import ProspectingLimits


def test_separate_caps_persist_and_refuse_before_next_action(tmp_path):
    first = ProspectingLimits(1, 2, str(tmp_path))
    first.reserve("invite")
    second = ProspectingLimits(1, 2, str(tmp_path))
    second.reserve("message")
    second.reserve("message")
    with pytest.raises(CodedToolError) as invite:
        second.reserve("invite")
    with pytest.raises(CodedToolError) as message:
        second.reserve("message")
    assert invite.value.code == message.value.code == ErrorCode.RATE_LIMITED
    assert invite.value.retry_after is not None and invite.value.retry_after > 0
    assert "retry_safe=true" in str(invite.value)
    assert invite.value.structured_content is not None
    assert invite.value.structured_content["retry_safe"] is True
    assert datetime.fromisoformat(
        invite.value.structured_content["reset_at"]
    ).date() == datetime.now().astimezone().date() + timedelta(days=1)


def test_release_and_local_day_reset(tmp_path):
    limit = ProspectingLimits(1, 1, str(tmp_path))
    token = limit.reserve("invite")
    limit.release(token)
    limit.reserve("invite")
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    with patch("linkedin_mcp_server.prospecting_limits.datetime") as clock:
        clock.now.return_value = tomorrow
        clock.combine = datetime.combine
        clock.min = datetime.min
        assert limit.reserve("invite")


def test_zero_disables_cap(tmp_path):
    limit = ProspectingLimits(0, 0, str(tmp_path))
    assert limit.reserve("invite") != limit.reserve("invite")
