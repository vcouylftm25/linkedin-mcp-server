"""CSV and JSON renderings of one campaign. No I/O of their own."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence

from linkedin_mcp_server.prospecting.domain import Lead

_COLUMNS = (
    "campaign_id",
    "lead_vanity",
    "kind",
    "name",
    "title",
    "company",
    "state",
    "note",
    "message_text",
    "profile_url",
    "updated_at",
)

# Cells a spreadsheet would treat as formulas when a lead's name starts this way.
_FORMULA = frozenset({"=", "+", "-", "@"})


def _cell(value: str) -> str:
    if value[:1] in _FORMULA:
        return "'" + value
    return value


def _row(lead: Lead) -> dict[str, str]:
    return {
        "campaign_id": lead.campaign_id,
        "lead_vanity": lead.lead_vanity,
        "kind": lead.kind,
        "name": lead.name,
        "title": lead.title or "",
        "company": lead.company or "",
        "state": lead.state.value,
        "note": lead.note or "",
        "message_text": lead.message_text or "",
        "profile_url": lead.profile_url,
        "updated_at": lead.updated_at.isoformat(),
    }


def to_csv(leads: Sequence[Lead]) -> str:
    """One row per lead, ordered by vanity."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for lead in sorted(leads, key=lambda item: (item.lead_vanity, item.kind)):
        writer.writerow({key: _cell(value) for key, value in _row(lead).items()})
    return buffer.getvalue()


def to_json(leads: Sequence[Lead]) -> str:
    """A JSON list of the same fields, ordered by vanity."""
    ordered = sorted(leads, key=lambda item: (item.lead_vanity, item.kind))
    rows = [_row(lead) for lead in ordered]
    return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
