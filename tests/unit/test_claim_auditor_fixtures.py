"""Known-fabrication fixtures for the report claim auditor.

Regression fixtures that simulate the four canonical fabrication classes the
auditor MUST flag as ``fabricated``:

1. Invented competitor / portfolio company name (seen in a production
   incident — "<firm> has backed X" where X is not in
   context.portfolio_companies).
2. Ticket band without source news ("typical check £500K-£1M" when
   context.news_articles has no source for the band).
3. Partner role hallucination ("Their partner Jane Doe leads FinTech"
   when context.partners has no Jane Doe).
4. Timing claim contradicting investment-timing metrics (report says
   "actively deploying" when
   context.timing_metrics.thesis_temperature is "cold").

Each test stubs the Gemini client to return the OUTPUT we expect a
well-prompted auditor to produce when shown the fabrication, then
asserts the audit pipeline reports it correctly. We are NOT
adversarially testing the LLM's extraction quality here — that's a
golden-output regression suite's job. What we ARE testing: when the
auditor flags a fabrication, the count + redaction + structured payload
behave correctly end-to-end.

CI-strip safety: every test in this file is pure-helper-level (uses
``audit_report_claims`` directly with a MagicMock client). No engine
imports. Gated on ``google.genai`` being present because the helper
itself does a lazy ``from google.genai import types`` inside the
function body.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


try:  # noqa: SIM105
    from google.genai import types as _real_genai_types  # noqa: F401
    _REAL_GENAI_PRESENT = hasattr(_real_genai_types, "GenerateContentConfig")
except Exception:
    _REAL_GENAI_PRESENT = False


from pierre.agents._claim_auditor import audit_report_claims


_SKIP_NO_REAL_GENAI = pytest.mark.skipif(
    not _REAL_GENAI_PRESENT,
    reason="fixtures-level test needs real google.genai (the auditor lazy-imports types)",
)


def _make_client(audits_payload: list[dict]) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.text = json.dumps({"audits": audits_payload})
    resp.usage_metadata = MagicMock(
        prompt_token_count=2000, candidates_token_count=300,
    )
    client.models.generate_content.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Fabrication 1 — Invented competitor / portfolio company name
# (the failure mode the auditor was built for).
# ---------------------------------------------------------------------------


@_SKIP_NO_REAL_GENAI
def test_invented_portfolio_company_flagged_as_fabricated(monkeypatch):
    """Report: 'Fourth Coffee has backed companies like Acme, Beta, GhostCo
    in this space.' Context portfolio contains only Acme + Beta. The
    auditor must flag the GhostCo claim as ``fabricated``."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "annotate")
    report_text = (
        "Fourth Coffee Ventures has backed companies like Acme, Beta, and "
        "GhostCo in this space, demonstrating a clear pattern of "
        "investment in the sector."
    )
    context = {
        "portfolio_companies": [
            {"name": "Acme"},
            {"name": "Beta"},
            # GhostCo deliberately missing
        ],
    }
    # Simulated well-prompted auditor output: 2 verified + 1 fabricated.
    client = _make_client(
        [
            {
                "claim_text": "Fourth Coffee Ventures has backed companies like Acme",
                "claim_type": "portfolio_company",
                "verdict": "verified",
                "evidence": "portfolio_companies[].name=Acme",
            },
            {
                "claim_text": "Beta",
                "claim_type": "portfolio_company",
                "verdict": "verified",
                "evidence": "portfolio_companies[].name=Beta",
            },
            {
                "claim_text": "and GhostCo in this space",
                "claim_type": "portfolio_company",
                "verdict": "fabricated",
                "evidence": None,
            },
        ]
    )
    result = audit_report_claims(report_text, context, client)
    assert result.fabricated_count == 1
    assert result.verified_count == 2
    # Redaction tags the fabricated claim — operator + UI can render
    # the caveat next to the name.
    assert "(fabricated)" in result.redacted_report


# ---------------------------------------------------------------------------
# Fabrication 2 — Ticket band without source news
# (a real fabrication from an early evaluation run).
# ---------------------------------------------------------------------------


@_SKIP_NO_REAL_GENAI
def test_ticket_band_without_source_news_flagged_as_fabricated(monkeypatch):
    """Report: 'Their typical check size is £500K-£1M at Seed.' Context
    has no news article with a ticket_size field that backs the band.
    The auditor must flag as ``fabricated``."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "annotate")
    report_text = (
        "Their typical check size is £500K-£1M at Seed. They have a "
        "strong reputation in the European fintech market."
    )
    context = {
        "news_articles": [
            # Two news rows, neither with a ticket_size that grounds the band.
            {"title": "Firm closes new fund", "intel_category": "fund_announcement"},
            {"title": "Portfolio company exits", "intel_category": "exit"},
        ],
        "portfolio_companies": [{"name": "SomeCo"}],
    }
    client = _make_client(
        [
            {
                "claim_text": "Their typical check size is £500K-£1M at Seed",
                "claim_type": "ticket_band",
                "verdict": "fabricated",
                "evidence": None,
            },
            {
                "claim_text": "strong reputation in the European fintech market",
                "claim_type": "other",
                "verdict": "unverifiable",
                "evidence": None,
            },
        ]
    )
    result = audit_report_claims(report_text, context, client)
    assert result.fabricated_count == 1
    assert result.unverifiable_count == 1
    # The fabricated ticket-band claim is annotated.
    assert "(fabricated)" in result.redacted_report
    # The unverifiable reputation claim gets the softer caveat.
    assert "(unverified)" in result.redacted_report


# ---------------------------------------------------------------------------
# Fabrication 3 — Partner role hallucination
# (a real fabrication from an early evaluation run).
# ---------------------------------------------------------------------------


@_SKIP_NO_REAL_GENAI
def test_partner_role_hallucination_flagged_as_fabricated(monkeypatch):
    """Report: 'Their partner Jane Doe leads FinTech investments.'
    Context.partners has no Jane Doe. The auditor must flag as
    ``fabricated``."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "strip")
    report_text = (
        "Their partner Jane Doe leads FinTech investments. The firm "
        "has a clear thesis on payments infrastructure."
    )
    context = {
        "partners": [
            {"name": "John Roe", "role": "Managing Partner"},
            {"name": "Alice Smith", "role": "Principal"},
            # Jane Doe absent
        ],
    }
    client = _make_client(
        [
            {
                "claim_text": "Their partner Jane Doe leads FinTech investments",
                "claim_type": "partner",
                "verdict": "fabricated",
                "evidence": None,
            },
        ]
    )
    result = audit_report_claims(report_text, context, client)
    assert result.fabricated_count == 1
    # Strip policy: the fabricated claim is fully replaced.
    assert "Jane Doe" not in result.redacted_report
    assert "[claim removed: not in context]" in result.redacted_report


# ---------------------------------------------------------------------------
# Fabrication 4 — timing claim contradicting investment-timing metrics.
# ---------------------------------------------------------------------------


@_SKIP_NO_REAL_GENAI
def test_timing_claim_contradicts_metrics_flagged_as_fabricated(monkeypatch):
    """Report: 'They are actively deploying in this sector.' Context
    timing_metrics says thesis_temperature='cold' with 0 investments in
    the last 90 days. The auditor must flag the cadence claim as
    ``fabricated``.

    Timing-related narrative phrasing is a newer fabrication surface; if
    the auditor didn't cover it, a new fabrication class would ship
    without a guard."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "annotate")
    report_text = (
        "They are actively deploying in this sector with strong "
        "momentum. The firm's thesis is well-aligned with the project."
    )
    context = {
        "timing_metrics": {
            "invest_count_90d": 0,
            "invest_count_365d": 1,
            "exit_count_365d": 0,
            "thesis_temperature": "cold",
            "days_since_last_invest": 410,
            "sectors_filter": None,
        },
        "portfolio_companies": [{"name": "SomeCo"}],
    }
    client = _make_client(
        [
            {
                "claim_text": "They are actively deploying in this sector",
                "claim_type": "timing_signal",
                "verdict": "fabricated",
                "evidence": "timing_metrics.thesis_temperature=cold",
            },
            {
                "claim_text": "thesis is well-aligned with the project",
                "claim_type": "other",
                "verdict": "unverifiable",
                "evidence": None,
            },
        ]
    )
    result = audit_report_claims(report_text, context, client)
    assert result.fabricated_count == 1
    assert any(
        a.claim_type == "timing_signal" and a.verdict == "fabricated"
        for a in result.audits
    ), (
        "Timing coverage regression — the timing_signal fabrication "
        "case did not surface as fabricated. Without this, the timing "
        "narrative cadence phrasing ships unguarded."
    )
    # Evidence string cites the timing_metrics path explicitly — useful
    # for operator triage in logs and the rendered report.
    fabricated_audit = next(
        a for a in result.audits if a.verdict == "fabricated"
    )
    assert "timing_metrics" in (fabricated_audit.evidence or "")
