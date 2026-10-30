"""Unit tests for ``pierre.agents._claim_auditor`` (pure helper).

Single-shot factcheck pass over the deep-report narrative. These tests cover:

- ``_summarize_context_for_prompt`` — shape coercion + drop of large
  evidence
- ``_truncate_report`` — token-budget guard
- ``_apply_redaction`` — strip / annotate / pass_through policies
- ``_safe_replace`` — whitespace-tolerant rewrite
- ``_estimate_cost_usd`` — Flash pricing math
- ``_parse_audits_payload`` — JSON tolerance + code-fence stripping +
  unknown-verdict coercion
- ``audit_report_claims`` — end-to-end with a MagicMock Gemini client,
  per-claim-type coverage (portfolio_company / news_citation / partner /
  ticket_band / timing_signal), empty-context short-circuit, Gemini
  failure pass-through

All tests run in a stripped-down environment without database drivers or
google.genai. The helper module imports only stdlib + typing +
``dataclasses``, and the mock client is a plain ``MagicMock``.

CI-strip safety guard: a module-top import probe sets a
``_REAL_GENAI_PRESENT`` flag, and tests that need the SDK are ``@skipif``-ed
on it — the auditor itself is pure, but the ``google.genai.types`` import
inside ``audit_report_claims`` requires the real SDK so we gate the live-call
test on it.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest


# CI-strip guard — only the live-call test needs google.genai installed.
try:  # noqa: SIM105
    from google.genai import types as _real_genai_types  # noqa: F401
    _REAL_GENAI_PRESENT = hasattr(_real_genai_types, "GenerateContentConfig")
except Exception:
    _REAL_GENAI_PRESENT = False


from pierre.agents._claim_auditor import (
    AuditResult,
    ClaimAudit,
    _apply_redaction,
    _build_audit_prompt,
    _estimate_cost_usd,
    _is_context_empty,
    _parse_audits_payload,
    _resolve_redact_policy,
    _safe_replace,
    _summarize_context_for_prompt,
    _truncate_report,
    audit_report_claims,
)


_SKIP_NO_REAL_GENAI = pytest.mark.skipif(
    not _REAL_GENAI_PRESENT,
    reason="behavioural test needs real google.genai (source-text/parse tests above cover the CI-strip case)",
)


# ---------------------------------------------------------------------------
# _summarize_context_for_prompt
# ---------------------------------------------------------------------------


class TestSummarizeContextForPrompt:
    def test_empty_context_returns_skeleton(self):
        out = _summarize_context_for_prompt({})
        assert out["portfolio_companies"] == []
        assert out["news_articles"] == []
        assert out["partners"] == []
        assert out["co_investors"] == []
        assert out["timing_metrics"] is None
        assert out["similar_firms"] == []
        assert out["thesis_clusters"] == []
        # The firm-level corpus keys
        # must be present (and empty) even on the empty-context skeleton,
        # so the prompt shape is stable across call paths.
        assert out["firm_geography"] == []
        assert out["firm_stages"] == []
        assert out["firm_check_size"] is None
        assert out["recent_deals"] == []

    def test_portfolio_companies_dict_shape_normalised(self):
        ctx = {
            "portfolio_companies": [
                {"company_name": "Acme", "description": "AI tools", "sector": "AI"},
                {"name": "BetaCo", "description": "X" * 500},  # description truncated
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert out["portfolio_companies"][0]["name"] == "Acme"
        assert out["portfolio_companies"][1]["name"] == "BetaCo"
        assert len(out["portfolio_companies"][1]["description"]) <= 200

    def test_portfolio_companies_string_entries_accepted(self):
        ctx = {"portfolio_companies": ["Acme", "BetaCo"]}
        out = _summarize_context_for_prompt(ctx)
        names = [p["name"] for p in out["portfolio_companies"]]
        assert names == ["Acme", "BetaCo"]

    def test_portfolio_evidence_fallback(self):
        # Engine sometimes passes portfolio_evidence (not portfolio_companies).
        ctx = {"portfolio_evidence": [{"company_name": "Acme"}]}
        out = _summarize_context_for_prompt(ctx)
        assert out["portfolio_companies"][0]["name"] == "Acme"

    def test_news_articles_truncated_title(self):
        ctx = {
            "news_articles": [
                {
                    "title": "Big news " * 50,  # ~450 chars
                    "url": "http://example.com",
                    "intel_category": "investment",
                    "ticket_size": "£10M",
                }
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["news_articles"][0]["title"]) <= 160
        assert out["news_articles"][0]["ticket_size"] == "£10M"

    def test_partners_dedup_by_name(self):
        ctx = {
            "partners": [
                {"name": "Jane Doe", "role": "Partner"},
                {"name": "jane doe", "role": "Senior Partner"},  # dup case-fold
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["partners"]) == 1
        assert out["partners"][0]["name"] == "Jane Doe"

    def test_partners_merges_knowledge_paths(self):
        ctx = {
            "partners": [{"name": "Jane Doe"}],
            "knowledge_paths": {
                "partners": [{"partner_name": "John Roe", "title": "MD"}],
                "co_investors": [{"firm_name": "Sequoia", "deal_count": 7}],
            },
        }
        out = _summarize_context_for_prompt(ctx)
        names = {p["name"] for p in out["partners"]}
        assert names == {"Jane Doe", "John Roe"}
        assert out["co_investors"][0]["name"] == "Sequoia"
        assert out["co_investors"][0]["deals"] == 7

    def test_timing_metrics_subset_carried_through(self):
        ctx = {
            "timing_metrics": {
                "invest_count_90d": 5,
                "invest_count_365d": 15,
                "exit_count_365d": 2,
                "thesis_temperature": "hot",
                "days_since_last_invest": 12,
                "sectors_filter": ["Fintech"],
                "invest_velocity_z": 2.5,  # NOT carried (rubric-leak hygiene)
            }
        }
        out = _summarize_context_for_prompt(ctx)
        tm = out["timing_metrics"]
        assert tm["thesis_temperature"] == "hot"
        assert tm["invest_count_90d"] == 5
        assert "invest_velocity_z" not in tm  # we drop the raw Z

    def test_similar_firms_and_thesis_clusters_capped(self):
        ctx = {
            "similar_firms": list(range(50)),  # cap at 20
            "thesis_clusters": list(range(25)),  # cap at 10
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["similar_firms"]) == 20
        assert len(out["thesis_clusters"]) == 10

    # -- firm-level corpus widening --
    #
    # These projection tests prove the auditor READS the new keys. The
    # WIRING (that the report engine puts them into audit_context) was
    # tested in the source system, whose report engine is not part of this
    # repo; the original bug was that the keys never reached the auditor.

    def test_firm_geography_and_stages_carried_through(self):
        ctx = {
            "firm_geography": ["Europe", "North America"],
            "firm_stages": ["Seed", "Series A"],
        }
        out = _summarize_context_for_prompt(ctx)
        assert out["firm_geography"] == ["Europe", "North America"]
        assert out["firm_stages"] == ["Seed", "Series A"]

    def test_firm_geography_and_stages_capped(self):
        ctx = {
            "firm_geography": [f"geo{i}" for i in range(40)],  # cap at 20
            "firm_stages": [f"stage{i}" for i in range(40)],  # cap at 20
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["firm_geography"]) == 20
        assert len(out["firm_stages"]) == 20

    def test_firm_check_size_dict_carried_through(self):
        ctx = {
            "firm_check_size": {
                "minimum": "$1M",
                "maximum": "$5M",
                "typical": None,
                "news_derived_band": {"low_eur": 1_000_000, "high_eur": 10_000_000},
            }
        }
        out = _summarize_context_for_prompt(ctx)
        assert out["firm_check_size"]["minimum"] == "$1M"
        assert out["firm_check_size"]["news_derived_band"]["high_eur"] == 10_000_000

    def test_firm_check_size_non_dict_coerced_to_none(self):
        out = _summarize_context_for_prompt({"firm_check_size": "$1M-$5M"})
        assert out["firm_check_size"] is None

    def test_recent_deals_dict_and_string_shapes(self):
        ctx = {
            "recent_deals": [
                {
                    "title": "Fourth Coffee backs Contoso Relay",
                    "url": "http://example.com/mr",
                    "excerpt": "X" * 500,  # excerpt truncated to 300
                },
                "Acme led Series A",
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert out["recent_deals"][0]["title"] == "Fourth Coffee backs Contoso Relay"
        assert len(out["recent_deals"][0]["excerpt"]) <= 300
        assert out["recent_deals"][1]["title"] == "Acme led Series A"

    def test_recent_deals_capped(self):
        ctx = {"recent_deals": [{"title": f"deal{i}"} for i in range(25)]}
        out = _summarize_context_for_prompt(ctx)
        assert len(out["recent_deals"]) == 10


# ---------------------------------------------------------------------------
# _truncate_report
# ---------------------------------------------------------------------------


class TestTruncateReport:
    def test_short_text_passes_through(self):
        text, was_truncated = _truncate_report("Hello world.")
        assert text == "Hello world."
        assert was_truncated is False

    def test_empty_text(self):
        text, was_truncated = _truncate_report("")
        assert text == ""
        assert was_truncated is False

    def test_long_text_truncated_and_marker_appended(self):
        long = "x" * 20_000
        text, was_truncated = _truncate_report(long, char_cap=16_000)
        assert was_truncated is True
        assert text.startswith("x" * 16_000)
        assert "truncated for audit token budget" in text


# ---------------------------------------------------------------------------
# _apply_redaction
# ---------------------------------------------------------------------------


def _make_audits():
    return [
        ClaimAudit(
            claim_text="They invested in Acme",
            claim_type="portfolio_company",
            verdict="verified",
            evidence="portfolio_companies[].name=Acme",
        ),
        ClaimAudit(
            claim_text="Their partner Jane Doe leads FinTech",
            claim_type="partner",
            verdict="fabricated",
            evidence=None,
        ),
        ClaimAudit(
            claim_text="They are known for deep-tech",
            claim_type="other",
            verdict="unverifiable",
            evidence=None,
        ),
    ]


class TestApplyRedaction:
    def test_pass_through_returns_original(self):
        report = "They invested in Acme. Their partner Jane Doe leads FinTech. They are known for deep-tech."
        out = _apply_redaction(report, _make_audits(), policy="pass_through")
        assert out == report

    def test_strip_replaces_fabricated_only(self):
        report = "They invested in Acme. Their partner Jane Doe leads FinTech. They are known for deep-tech."
        out = _apply_redaction(report, _make_audits(), policy="strip")
        assert "Their partner Jane Doe leads FinTech" not in out
        assert "[claim removed: not in context]" in out
        # Verified + unverifiable untouched in strip policy.
        assert "They invested in Acme" in out
        assert "They are known for deep-tech" in out

    def test_annotate_tags_fabricated_and_unverifiable(self):
        report = "They invested in Acme. Their partner Jane Doe leads FinTech. They are known for deep-tech."
        out = _apply_redaction(report, _make_audits(), policy="annotate")
        assert "(fabricated)" in out
        assert "(unverified)" in out
        # Verified claim unchanged.
        assert "They invested in Acme." in out

    def test_empty_report_returns_empty(self):
        assert _apply_redaction("", _make_audits(), "strip") == ""

    def test_no_audits_returns_original(self):
        assert _apply_redaction("Some text", [], "strip") == "Some text"


class TestSafeReplace:
    def test_exact_match(self):
        assert _safe_replace("hello world", "world", "earth") == "hello earth"

    def test_no_match_returns_original(self):
        assert _safe_replace("hello world", "absent", "x") == "hello world"

    def test_whitespace_tolerant(self):
        # Source has double-space, needle has single-space.
        out = _safe_replace("hello  world", "hello world", "X")
        assert out == "X"

    def test_empty_needle_returns_original(self):
        assert _safe_replace("hello", "", "X") == "hello"


# ---------------------------------------------------------------------------
# _estimate_cost_usd
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_typical_audit_cost_under_half_a_cent(self):
        # 4k input + 500 output is a realistic audit ceiling (~$0.0025).
        cost = _estimate_cost_usd(4_000, 500)
        assert cost < 0.005, f"Audit cost {cost:.6f} exceeds $0.005 cap"

    def test_zero_tokens_is_zero(self):
        assert _estimate_cost_usd(0, 0) == 0.0


# ---------------------------------------------------------------------------
# _parse_audits_payload
# ---------------------------------------------------------------------------


class TestParseAuditsPayload:
    def test_empty_input_returns_empty_list(self):
        assert _parse_audits_payload("") == []

    def test_well_formed_json_parses(self):
        payload = json.dumps(
            {
                "audits": [
                    {
                        "claim_text": "X invested in Acme",
                        "claim_type": "portfolio_company",
                        "verdict": "verified",
                        "evidence": "portfolio_companies[].name=Acme",
                    }
                ]
            }
        )
        audits = _parse_audits_payload(payload)
        assert len(audits) == 1
        assert audits[0].claim_text == "X invested in Acme"
        assert audits[0].verdict == "verified"

    def test_code_fence_wrapped_json_parses(self):
        payload = (
            "```json\n"
            '{"audits": [{"claim_text": "foo", "claim_type": "other", "verdict": "fabricated", "evidence": null}]}\n'
            "```"
        )
        audits = _parse_audits_payload(payload)
        assert len(audits) == 1
        assert audits[0].verdict == "fabricated"
        assert audits[0].evidence is None

    def test_unknown_verdict_coerced_to_unverifiable(self):
        payload = json.dumps(
            {
                "audits": [
                    {
                        "claim_text": "foo",
                        "claim_type": "other",
                        "verdict": "garbage",
                    }
                ]
            }
        )
        audits = _parse_audits_payload(payload)
        assert audits[0].verdict == "unverifiable"

    def test_unknown_claim_type_coerced_to_other(self):
        payload = json.dumps(
            {
                "audits": [
                    {
                        "claim_text": "foo",
                        "claim_type": "nonsense",
                        "verdict": "verified",
                    }
                ]
            }
        )
        audits = _parse_audits_payload(payload)
        assert audits[0].claim_type == "other"

    def test_malformed_json_returns_empty(self):
        assert _parse_audits_payload("not json at all") == []

    def test_non_dict_payload_returns_empty(self):
        assert _parse_audits_payload('["just", "an", "array"]') == []

    def test_audits_not_a_list_returns_empty(self):
        assert _parse_audits_payload('{"audits": "string"}') == []

    def test_missing_claim_text_skipped(self):
        payload = json.dumps(
            {
                "audits": [
                    {"claim_text": "", "claim_type": "other", "verdict": "verified"},
                    {"claim_text": "real", "claim_type": "other", "verdict": "verified"},
                ]
            }
        )
        audits = _parse_audits_payload(payload)
        assert len(audits) == 1
        assert audits[0].claim_text == "real"


# ---------------------------------------------------------------------------
# _is_context_empty
# ---------------------------------------------------------------------------


class TestIsContextEmpty:
    def test_empty_skeleton_is_empty(self):
        assert _is_context_empty(_summarize_context_for_prompt({}))

    def test_portfolio_present_is_not_empty(self):
        s = _summarize_context_for_prompt({"portfolio_companies": [{"name": "X"}]})
        assert not _is_context_empty(s)

    def test_timing_metrics_present_is_not_empty(self):
        s = _summarize_context_for_prompt(
            {"timing_metrics": {"thesis_temperature": "hot"}}
        )
        assert not _is_context_empty(s)


# ---------------------------------------------------------------------------
# _resolve_redact_policy
# ---------------------------------------------------------------------------


class TestResolveRedactPolicy:
    def test_default_is_annotate(self, monkeypatch):
        monkeypatch.delenv("RCF_REDACT_POLICY", raising=False)
        assert _resolve_redact_policy() == "annotate"

    def test_strip_respected(self, monkeypatch):
        monkeypatch.setenv("RCF_REDACT_POLICY", "strip")
        assert _resolve_redact_policy() == "strip"

    def test_pass_through_respected(self, monkeypatch):
        monkeypatch.setenv("RCF_REDACT_POLICY", "pass_through")
        assert _resolve_redact_policy() == "pass_through"

    def test_unknown_falls_back_to_annotate(self, monkeypatch):
        monkeypatch.setenv("RCF_REDACT_POLICY", "garbage")
        assert _resolve_redact_policy() == "annotate"


# ---------------------------------------------------------------------------
# _build_audit_prompt — light structural assertions only.
# ---------------------------------------------------------------------------


class TestBuildAuditPrompt:
    def test_prompt_contains_required_sections(self):
        prompt = _build_audit_prompt(
            "Sample report",
            _summarize_context_for_prompt({"portfolio_companies": [{"name": "Acme"}]}),
        )
        assert "REPORT NARRATIVE" in prompt
        assert "CONTEXT DICT" in prompt
        assert "Sample report" in prompt
        assert '"Acme"' in prompt  # JSON-encoded context entry
        assert "verified" in prompt
        assert "fabricated" in prompt
        assert "unverifiable" in prompt
        # Audit prompt MUST teach the LLM about timing_signal — timing
        # claims are a known fabrication surface.
        assert "timing_signal" in prompt
        assert "timing_metrics" in prompt


# ---------------------------------------------------------------------------
# audit_report_claims — end-to-end with a MagicMock Gemini client.
# ---------------------------------------------------------------------------


def _make_mock_client(
    audits_payload: list[dict],
    input_tokens: int = 1500,
    output_tokens: int = 200,
) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.text = json.dumps({"audits": audits_payload})
    resp.usage_metadata = MagicMock(
        prompt_token_count=input_tokens,
        candidates_token_count=output_tokens,
    )
    client.models.generate_content.return_value = resp
    return client


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_short_circuits_on_empty_report():
    """Empty narrative → no Gemini call, zero-count result."""
    client = MagicMock()
    out = audit_report_claims("", {"portfolio_companies": [{"name": "Acme"}]}, client)
    assert isinstance(out, AuditResult)
    assert out.verified_count == 0
    assert out.fabricated_count == 0
    assert out.audit_cost_usd == 0.0
    client.models.generate_content.assert_not_called()


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_short_circuits_on_empty_context():
    """Empty context → no audit fires (nothing to verify against)."""
    client = MagicMock()
    out = audit_report_claims("Some report text", {}, client)
    assert out.verified_count == 0
    assert out.fabricated_count == 0
    assert out.redacted_report == "Some report text"  # pass-through
    client.models.generate_content.assert_not_called()


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_all_verified():
    """Happy path: every claim verified → counts match, no rewrite."""
    client = _make_mock_client(
        [
            {
                "claim_text": "Backed Acme",
                "claim_type": "portfolio_company",
                "verdict": "verified",
                "evidence": "portfolio_companies[].name=Acme",
            },
        ]
    )
    context = {"portfolio_companies": [{"name": "Acme"}]}
    out = audit_report_claims("They backed Acme.", context, client)
    assert out.verified_count == 1
    assert out.fabricated_count == 0
    assert out.unverifiable_count == 0
    assert out.audit_cost_usd > 0


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_per_claim_type_coverage():
    """All 5 claim types are accepted and counted correctly."""
    client = _make_mock_client(
        [
            {
                "claim_text": "Portfolio claim",
                "claim_type": "portfolio_company",
                "verdict": "verified",
            },
            {
                "claim_text": "News claim",
                "claim_type": "news_citation",
                "verdict": "verified",
            },
            {
                "claim_text": "Partner claim",
                "claim_type": "partner",
                "verdict": "fabricated",
            },
            {
                "claim_text": "Ticket claim",
                "claim_type": "ticket_band",
                "verdict": "fabricated",
            },
            {
                "claim_text": "Timing claim",
                "claim_type": "timing_signal",
                "verdict": "unverifiable",
            },
        ]
    )
    context = {
        "portfolio_companies": [{"name": "Acme"}],
        "news_articles": [{"title": "news"}],
        "timing_metrics": {"thesis_temperature": "hot"},
    }
    out = audit_report_claims(
        "Portfolio claim. News claim. Partner claim. Ticket claim. Timing claim.",
        context,
        client,
    )
    assert out.verified_count == 2
    assert out.fabricated_count == 2
    assert out.unverifiable_count == 1
    assert len(out.audits) == 5
    types_seen = {a.claim_type for a in out.audits}
    assert types_seen == {
        "portfolio_company",
        "news_citation",
        "partner",
        "ticket_band",
        "timing_signal",
    }


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_gemini_failure_returns_pass_through():
    """Gemini raises → AuditResult with zero counts, original text intact."""
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("Gemini boom")
    context = {"portfolio_companies": [{"name": "Acme"}]}
    out = audit_report_claims("Report text", context, client)
    assert out.verified_count == 0
    assert out.fabricated_count == 0
    assert out.redacted_report == "Report text"
    assert out.audit_cost_usd == 0.0


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_cost_log_callback_fires():
    """The cost_log_callback receives (cost_usd, in_tokens, out_tokens)."""
    client = _make_mock_client(
        [
            {
                "claim_text": "x",
                "claim_type": "other",
                "verdict": "verified",
            }
        ],
        input_tokens=1000,
        output_tokens=100,
    )
    context = {"portfolio_companies": [{"name": "Acme"}]}

    received: list = []

    def _cb(cost: float, in_tok: int, out_tok: int) -> None:
        received.append((cost, in_tok, out_tok))

    audit_report_claims("x", context, client, cost_log_callback=_cb)
    assert len(received) == 1
    cost, in_tok, out_tok = received[0]
    assert in_tok == 1000
    assert out_tok == 100
    assert cost > 0


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_cost_log_callback_failure_swallowed():
    """A failing callback must not break the audit."""
    client = _make_mock_client(
        [{"claim_text": "x", "claim_type": "other", "verdict": "verified"}]
    )
    context = {"portfolio_companies": [{"name": "Acme"}]}

    def _bad(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cb boom")

    # Must NOT raise.
    out = audit_report_claims("x", context, client, cost_log_callback=_bad)
    assert out.verified_count == 1


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_annotate_policy_rewrites_text(monkeypatch):
    """Default annotate policy adds (fabricated) / (unverified) caveats."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "annotate")
    client = _make_mock_client(
        [
            {
                "claim_text": "Their partner Jane Doe leads FinTech",
                "claim_type": "partner",
                "verdict": "fabricated",
            }
        ]
    )
    context = {"partners": [{"name": "John Roe", "role": "MD"}]}
    out = audit_report_claims(
        "Their partner Jane Doe leads FinTech. They are well-known.",
        context,
        client,
    )
    assert "(fabricated)" in out.redacted_report


@_SKIP_NO_REAL_GENAI
def test_audit_report_claims_strip_policy_replaces_text(monkeypatch):
    """Strip policy fully removes the fabricated claim."""
    monkeypatch.setenv("RCF_REDACT_POLICY", "strip")
    client = _make_mock_client(
        [
            {
                "claim_text": "Their partner Jane Doe leads FinTech",
                "claim_type": "partner",
                "verdict": "fabricated",
            }
        ]
    )
    context = {"partners": [{"name": "John Roe"}]}
    out = audit_report_claims(
        "Their partner Jane Doe leads FinTech. They are well-known.",
        context,
        client,
    )
    assert "Their partner Jane Doe leads FinTech" not in out.redacted_report
    assert "[claim removed: not in context]" in out.redacted_report


# ---------------------------------------------------------------------------
# Partner focus_sectors projection
# ---------------------------------------------------------------------------


class TestPartnerFocusSectorsProjection:
    """_summarize_context_for_prompt MUST project focus_sectors per partner.

    Previously partners.append only carried {"name": name, "role": role},
    silently dropping focus_sectors even though the context builder already
    fetches it.  A claim like "<partner> focuses on Consumer" was stamped
    fabricated even though
    focus_sectors=['Consumer'] is verbatim in the DB row.
    """

    def test_focus_sectors_list_projected(self):
        """focus_sectors list passes through onto the partner dict."""
        ctx = {
            "partners": [
                {
                    "name": "Jane Doe",
                    "role": "Partner",
                    "focus_sectors": ["Consumer", "Retail"],
                }
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["partners"]) == 1
        p = out["partners"][0]
        assert p["name"] == "Jane Doe"
        assert p["focus_sectors"] == ["Consumer", "Retail"]

    def test_focus_sectors_string_coerced_to_list(self):
        """A bare string focus_sectors is wrapped in a list."""
        ctx = {
            "partners": [
                {"name": "Alex Smith", "role": "MD", "focus_sectors": "FinTech"}
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert out["partners"][0]["focus_sectors"] == ["FinTech"]

    def test_focus_sectors_absent_key_not_added(self):
        """Partners without focus_sectors must NOT have the key added (no noise)."""
        ctx = {"partners": [{"name": "Bob Jones", "role": "Associate"}]}
        out = _summarize_context_for_prompt(ctx)
        assert "focus_sectors" not in out["partners"][0]

    def test_focus_sectors_empty_list_not_added(self):
        """Empty focus_sectors list must NOT be projected (no noise)."""
        ctx = {"partners": [{"name": "Alice Brown", "focus_sectors": []}]}
        out = _summarize_context_for_prompt(ctx)
        assert "focus_sectors" not in out["partners"][0]

    def test_focus_sectors_from_knowledge_paths(self):
        """focus_sectors carried inside knowledge_paths.partners is projected."""
        ctx = {
            "knowledge_paths": {
                "partners": [
                    {
                        "partner_name": "Dana Torres",
                        "title": "Principal",
                        "focus_sectors": ["DeepTech", "Climate"],
                    }
                ]
            }
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["partners"]) == 1
        assert out["partners"][0]["focus_sectors"] == ["DeepTech", "Climate"]

    def test_prompt_rule_4_mentions_focus_sectors(self):
        """_build_audit_prompt rule #4 must explicitly reference focus_sectors
        so the LLM knows to cross-check a partner sector claim."""
        prompt = _build_audit_prompt("Some narrative.", {})
        assert "focus_sectors" in prompt


# ---------------------------------------------------------------------------
# recent_deals from news
# ---------------------------------------------------------------------------
#
# NOTE: populating recent_deals from the full news evidence is WIRING in the
# source system's report generator, not in the pure _claim_auditor.py helper,
# and is tested there.
#
# What we prove here is the projection side: _summarize_context_for_prompt
# carries ALL recent_deals entries through correctly including the new
# name-matching fields (intel_category, company_mentioned) it now passes
# through alongside title/url/excerpt.


class TestRecentDealsNewsWiring:
    """Projection tests for the richer recent_deals shape."""

    def test_recent_deals_intel_category_and_company_mentioned_carried(self):
        """The name-matching fields written by the report engine reach the prompt."""
        ctx = {
            "recent_deals": [
                {
                    "title": "JamJar backs Tewke",
                    "url": "http://example.com/tewke",
                    "excerpt": "JamJar Investments has backed Tewke.",
                    "intel_category": "investment",
                    "company_mentioned": "Tewke",
                }
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        # intel_category and company_mentioned MUST be carried
        # through so a deal-mention claim whose company name lives in
        # company_mentioned (not the title) is still groundable.
        assert len(out["recent_deals"]) == 1
        rd = out["recent_deals"][0]
        assert rd["title"] == "JamJar backs Tewke"
        assert rd["intel_category"] == "investment"
        assert rd["company_mentioned"] == "Tewke"

    def test_recent_deals_capped_at_ten(self):
        """When news_evidence is large the cap prevents prompt bloat."""
        ctx = {
            "recent_deals": [
                {"title": f"Deal {i}", "url": f"http://example.com/{i}"}
                for i in range(20)
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["recent_deals"]) == 10

    def test_recent_deals_excerpt_truncated(self):
        """Long excerpts are trimmed to 300 chars."""
        ctx = {
            "recent_deals": [
                {
                    "title": "Investment note",
                    "excerpt": "x" * 600,
                }
            ]
        }
        out = _summarize_context_for_prompt(ctx)
        assert len(out["recent_deals"][0]["excerpt"]) <= 300
