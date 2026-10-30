"""Pure helper for post-narrative claim auditing (RCF — Report Claim FactCheck).

Re-read every deep-report narrative and verify every factual claim
against the structured ``context`` dict the report engine already
assembled. Catches LLM fabrications — invented portfolio companies, ticket bands without source news, hallucinated
partner roles, timing claims that contradict ``timing_metrics``.

Architecture:

- Pure stdlib + ``typing`` + ``dataclasses``. No database driver / ORM
  imports. NO ``google.genai`` import at module top — the caller threads a
  live client through, so the module imports in a stripped-down
  dependency set.
- ONE Gemini Flash call per report — extract every claim + cross-check
  in a single round-trip. No per-claim fan-out (cost explosion).
- Three verdict states per claim: ``verified`` (matches a context
  entry), ``fabricated`` (references a name/value absent from the
  context), ``unverifiable`` (plausible but context lacks the evidence
  to confirm or refute — e.g. vibe assertions like "known for
  deep-tech").
- Redaction policy is operator-configurable via the ``RCF_REDACT_POLICY``
  env var: ``strip`` (replace fabricated claims with placeholder),
  ``annotate`` (default: tag ``(fabricated)`` / ``(unverified)`` claims), or
  ``pass_through`` (no rewrite, only structured audit returned).
- Token-budget guard: caps the auditor input at ~4k tokens of report
  text to keep cost around $0.0025/report on Flash. Excess is truncated with
  a marker, and the audit summary flags ``budget_truncated=True``.
- Best-effort but observable: a failing Gemini call logs a WARNING and
  returns a pass-through result rather than failing silently or breaking
  the report.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ClaimVerdict = Literal["verified", "fabricated", "unverifiable"]
ClaimType = Literal[
    "portfolio_company",
    "news_citation",
    "partner",
    "ticket_band",
    "timing_signal",
    "other",
]
RedactPolicy = Literal["strip", "annotate", "pass_through"]


@dataclass(frozen=True)
class ClaimAudit:
    """Per-claim audit record returned by :func:`audit_report_claims`.

    Attributes:
        claim_text: Verbatim or near-verbatim snippet of the claim from
            the narrative.
        claim_type: Coarse category — drives the verification rule used.
        verdict: ``verified`` / ``fabricated`` / ``unverifiable``.
        evidence: Optional context-dict reference that verified / refuted
            the claim. Empty for ``unverifiable``.
    """

    claim_text: str
    claim_type: ClaimType
    verdict: ClaimVerdict
    evidence: Optional[str] = None


@dataclass(frozen=True)
class AuditResult:
    """Aggregate result returned by :func:`audit_report_claims`.

    Attributes:
        verified_count: Number of claims that matched a context entry.
        fabricated_count: Number of claims that referenced a name/value
            absent from the context.
        unverifiable_count: Number of plausible-but-unprovable claims.
        audits: Full per-claim record.
        redacted_report: The report text with fabricated claims handled
            per ``RCF_REDACT_POLICY`` (strip / annotate / pass_through).
        budget_truncated: True when the input report exceeded the
            token-budget cap and was truncated before auditing.
        audit_cost_usd: Cost of the single Gemini Flash audit call.
            0.0 when the helper short-circuited (empty report / empty
            context / Gemini failure caught upstream).
    """

    verified_count: int
    fabricated_count: int
    unverifiable_count: int
    audits: list = field(default_factory=list)
    redacted_report: str = ""
    budget_truncated: bool = False
    audit_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Gemini client protocol — keeps the module CI-strip safe by not importing
# google.genai at the top level. Caller threads a real client through.
# ---------------------------------------------------------------------------


class _GeminiClientProtocol(Protocol):
    """Minimal shape we exercise on the Gemini client.

    The real ``google.genai.Client`` exposes ``client.models.generate_content``;
    we duck-type that shape so unit tests can pass a ``MagicMock`` without
    needing the SDK installed.
    """

    models: Any  # has `.generate_content(model=..., contents=..., config=...)`


# ---------------------------------------------------------------------------
# Tunables — kept here so the pure helper has zero external config imports.
# ---------------------------------------------------------------------------

# Token-budget cap on the input report text. ~4k tokens ≈ ~16k chars.
# Beyond this we truncate and flag ``budget_truncated=True``. At the Gemini 2.5
# Flash rates below, a typical audit (~4k tokens in, ~500 out) costs ~$0.0025.
_REPORT_TEXT_CHAR_CAP = 16_000

# Gemini 2.5 Flash pricing (USD per 1M tokens), the same rates as
# ``pierre/utils/llm_cost.py``. Local constants keep this module import-light;
# the figure is observability only (a cost tally, not a budget gate).
_GEMINI_FLASH_INPUT_PER_1M = 0.30
_GEMINI_FLASH_OUTPUT_PER_1M = 2.50


# ---------------------------------------------------------------------------
# Pure helpers — context shaping + redaction. Tested standalone.
# ---------------------------------------------------------------------------


def _summarize_context_for_prompt(context: dict) -> dict:
    """Project ``context`` into a compact verification corpus.

    Keeps only the fields the auditor actually cross-checks against,
    drops embedding blobs / large evidence arrays. Stable shape so the
    LLM prompt is deterministic across runs.

    The returned dict is JSON-serialised into the prompt — keep it small.
    """
    if not context:
        return {
            "portfolio_companies": [],
            "news_articles": [],
            "partners": [],
            "co_investors": [],
            "timing_metrics": None,
            "similar_firms": [],
            "thesis_clusters": [],
            "firm_geography": [],
            "firm_stages": [],
            "firm_focus_areas": [],
            "firm_check_size": None,
            "firm_hq": None,
            "firm_description": None,
            "firm_foundation_date": None,
            "recent_deals": [],
            "cluster_patterns": None,
        }

    # ``portfolio_companies`` may live under context['portfolio_companies']
    # OR as a flat ``portfolio_evidence`` list — the engine populates one
    # or the other depending on the call path. We accept both shapes.
    raw_portfolio = (
        context.get("portfolio_companies")
        or context.get("portfolio_evidence")
        or []
    )
    portfolio: list = []
    for entry in raw_portfolio:
        if isinstance(entry, dict):
            name = entry.get("company_name") or entry.get("name") or ""
            if name:
                portfolio.append(
                    {
                        "name": str(name),
                        "description": (entry.get("description") or "")[:200],
                        "sector": entry.get("sector") or entry.get("sector_tag"),
                    }
                )
        elif isinstance(entry, str) and entry.strip():
            portfolio.append({"name": entry.strip()})

    raw_news = context.get("news_articles") or context.get("news_evidence") or []
    news: list = []
    for entry in raw_news:
        if not isinstance(entry, dict):
            continue
        news.append(
            {
                "title": (entry.get("title") or "")[:160],
                "url": entry.get("url"),
                "intel_category": entry.get("intel_category"),
                "ticket_size": entry.get("ticket_size") or entry.get("round_size"),
                # The article's own date. Without it, ANY claim that dates a
                # deal ("X raised $41M Series C in July 2022") lands
                # unverifiable even though the article is right there in the
                # corpus — the auditor could confirm the round but never the
                # when. Same bug class as the firm-level blocks below: data
                # present in the database that the projection drops. Measured
                # on a live firm dossier: 9 of 10 false-unverifiables were
                # dated news claims.
                "publication_date": entry.get("publication_date")
                or entry.get("published_at"),
            }
        )

    # Partners may live under context['partners'] OR be carried in
    # ``knowledge_paths['partners']`` (engine path). Merge both.
    raw_partners = list(context.get("partners") or [])
    kp = context.get("knowledge_paths") or {}
    if isinstance(kp, dict):
        raw_partners.extend(kp.get("partners") or [])

    partners: list = []
    seen_partner_names: set = set()
    for entry in raw_partners:
        if isinstance(entry, dict):
            name = (entry.get("name") or entry.get("partner_name") or "").strip()
            role = (entry.get("role") or entry.get("title") or "").strip()
        elif isinstance(entry, str):
            name, role = entry.strip(), ""
        else:
            continue
        if not name or name.lower() in seen_partner_names:
            continue
        seen_partner_names.add(name.lower())
        # Project focus_sectors so that "<partner> focuses on Consumer" is
        # grounded against the partner record (focus_sectors=['Consumer'])
        # rather than stamped fabricated. Without this projection the field
        # was silently dropped here even though the context builder fetches it.
        focus_sectors = entry.get("focus_sectors") if isinstance(entry, dict) else None
        partner_entry: dict = {"name": name, "role": role}
        if focus_sectors:
            partner_entry["focus_sectors"] = (
                list(focus_sectors)
                if isinstance(focus_sectors, list)
                else [str(focus_sectors)]
            )
        partners.append(partner_entry)

    # Co-investors may live under context['co_investors'] (top-level, the
    # fixture and calibration path) OR under knowledge_paths['co_investors']
    # (the report engine's path). Merge both so the auditor sees the full list
    # regardless of which call path was used. Without this, top-level
    # co_investors is silently dropped (empty kp → co_investors_raw=[] → the
    # co-investor never reaches the prompt — a defect the calibration harness
    # surfaced).
    co_investors_raw = list(context.get("co_investors") or [])
    if isinstance(kp, dict):
        co_investors_raw.extend(kp.get("co_investors") or [])
    co_investors: list = []
    for entry in co_investors_raw:
        if isinstance(entry, dict):
            name = (entry.get("firm_name") or entry.get("name") or "").strip()
            if name:
                co_investors.append({"name": name, "deals": entry.get("deal_count")})
        elif isinstance(entry, str) and entry.strip():
            co_investors.append({"name": entry.strip()})

    # Investment-timing metrics — a common fabrication surface.
    timing = context.get("timing_metrics")
    timing_for_prompt: Optional[dict] = None
    if isinstance(timing, dict):
        timing_for_prompt = {
            "invest_count_90d": timing.get("invest_count_90d"),
            "invest_count_365d": timing.get("invest_count_365d"),
            "exit_count_365d": timing.get("exit_count_365d"),
            "thesis_temperature": timing.get("thesis_temperature"),
            "days_since_last_invest": timing.get("days_since_last_invest"),
            "sectors_filter": timing.get("sectors_filter"),
        }

    similar_firms = context.get("similar_firms") or []
    thesis_clusters = context.get("thesis_clusters") or []

    # Firm-level data the narrative was generated from, threaded in by the
    # report engine. These
    # are FIRM attributes, distinct from the startup's autoclassified
    # geography/stages. Without them, real narrative-used data (a firm's
    # stated/derived check-size band, its geography & stages, a recent-deal
    # rationale snippet) gets stamped fabricated because the auditor never
    # saw it. ``firm_check_size`` may carry an explicit range AND/OR a
    # news-derived band (an inference, surfaced so the auditor accepts an
    # inference-labelled check-size claim instead of flagging it).
    firm_geography = context.get("firm_geography")
    firm_geography = firm_geography if isinstance(firm_geography, list) else []
    firm_stages = context.get("firm_stages")
    firm_stages = firm_stages if isinstance(firm_stages, list) else []
    # Firm-declared focus areas. Distinct from portfolio sector tags — these
    # are the firm's own stated investment themes (e.g. "FoodTech", "Impact").
    # Without this, a claim asserting a focus area the firm explicitly lists
    # lands unverifiable even though the data exists in the database.
    firm_focus_areas = context.get("firm_focus_areas")
    firm_focus_areas = firm_focus_areas if isinstance(firm_focus_areas, list) else []
    firm_check_size = context.get("firm_check_size")
    if not isinstance(firm_check_size, dict):
        firm_check_size = None
    # The firm's HQ and its own stated description. Same bug class as the two
    # blocks above. A claim like "<firm> is headquartered in London, UK" cites
    # the firm's stored HQ — present in the database, invisible to the
    # auditor, so it landed unverifiable on a live dossier. `firm_description` is the firm's own
    # copy (founding year, origin story, self-described thesis); narratives
    # legitimately draw on it and every such claim was being marked doubtful.
    # Truncated: it is free prose, and the projection must stay small.
    firm_hq = context.get("firm_hq") or None
    if firm_hq is not None and not isinstance(firm_hq, str):
        firm_hq = str(firm_hq)
    firm_description = context.get("firm_description")
    firm_description = (
        firm_description[:600] if isinstance(firm_description, str) and firm_description
        else None
    )
    firm_foundation_date = context.get("firm_foundation_date") or None
    if firm_foundation_date is not None and not isinstance(firm_foundation_date, str):
        firm_foundation_date = str(firm_foundation_date)

    recent_deals_raw = context.get("recent_deals") or []
    recent_deals: list = []
    for entry in recent_deals_raw:
        if isinstance(entry, dict):
            recent_deals.append(
                {
                    "title": (entry.get("title") or "")[:160],
                    "url": entry.get("url"),
                    "excerpt": (entry.get("excerpt") or "")[:300],
                    # Carry the name-matching fields so a deal-mention claim whose
                    # company name lives in company_mentioned (not the title)
                    # is still groundable. Mirrors the news_articles
                    # projection above which already carries intel_category.
                    "intel_category": entry.get("intel_category"),
                    "company_mentioned": entry.get("company_mentioned"),
                }
            )
        elif isinstance(entry, str) and entry.strip():
            recent_deals.append({"title": entry.strip()[:160]})

    # Portfolio cluster-pattern analysis
    # (business_model / gtm_approach / customer_segment / subsector /
    # stage_maturity). Project the dominant_pattern + count + prevalence per
    # dimension so the auditor can ground claims like "the firm's portfolio
    # shows a moderate clustering pattern for b2b_enterprise". Without this
    # projection all such claims landed unverifiable (16/16 in a measurement,
    # all wrongly). Mirrors the thesis_clusters projection above.
    raw_cluster_patterns = context.get("cluster_patterns")
    cluster_patterns_for_prompt: Optional[dict] = None
    if isinstance(raw_cluster_patterns, dict):
        compacted: dict = {}
        for dim, dim_data in raw_cluster_patterns.items():
            if isinstance(dim_data, dict) and dim_data.get("dominant_pattern"):
                compacted[dim] = {
                    "dominant_pattern": dim_data.get("dominant_pattern"),
                    "count": dim_data.get("count"),
                    "prevalence": dim_data.get("prevalence"),
                    "severity": dim_data.get("severity"),
                    "startup_matches": dim_data.get("startup_matches"),
                }
        if compacted:
            cluster_patterns_for_prompt = compacted

    return {
        "portfolio_companies": portfolio,
        "news_articles": news,
        "partners": partners,
        "co_investors": co_investors,
        "timing_metrics": timing_for_prompt,
        "similar_firms": similar_firms[:20],
        "thesis_clusters": thesis_clusters[:10],
        "firm_geography": firm_geography[:20],
        "firm_stages": firm_stages[:20],
        "firm_focus_areas": firm_focus_areas[:15],
        "firm_check_size": firm_check_size,
        "firm_hq": firm_hq,
        "firm_description": firm_description,
        "firm_foundation_date": firm_foundation_date,
        "recent_deals": recent_deals[:10],
        "cluster_patterns": cluster_patterns_for_prompt,
    }


def _truncate_report(report_text: str, char_cap: int = _REPORT_TEXT_CHAR_CAP) -> tuple:
    """Return ``(truncated_text, was_truncated)``.

    Caps at the top-3 paragraphs heuristically when over budget — the
    rest is replaced with a single marker line so the LLM knows it was
    cut.
    """
    if not report_text:
        return "", False
    if len(report_text) <= char_cap:
        return report_text, False
    truncated = report_text[:char_cap]
    return (
        truncated
        + "\n\n[...remainder of report truncated for audit token budget. "
        "Audit covers the first portion only.]",
        True,
    )


def _build_audit_prompt(report_text: str, context_summary: dict) -> str:
    """Compose the single-shot extract-and-verify prompt.

    The output spec is a JSON object so the caller can ``json.loads``
    without regex. The LLM is told explicitly to NOT extract claims that
    are matters of pure opinion / phrasing — only factual, name-bearing
    or number-bearing assertions.
    """
    context_json = json.dumps(context_summary, indent=2, default=str)
    return (
        "You are a fact-checking auditor for a VC match report.\n\n"
        "Below is the REPORT NARRATIVE the system generated, followed by the\n"
        "CONTEXT DICT that was passed to the narrative LLM. Every factual\n"
        "claim in the narrative MUST be grounded in the context dict. Your\n"
        "job: extract every factual claim from the narrative and assign one\n"
        "of three verdicts:\n\n"
        "- verified: the claim names a portfolio company / news source /\n"
        "  partner / ticket band / timing signal that EXISTS in the context\n"
        "  dict and matches the claim's substance.\n"
        "- fabricated: the claim names something (a company, a person, a\n"
        "  number) that is NOT present in the context dict.\n"
        "- unverifiable: the claim is plausible but the context dict does\n"
        "  not have enough evidence to confirm OR refute it (e.g. vague\n"
        "  characterisations like \"known for deep-tech\" with no\n"
        "  supporting context entry).\n\n"
        "PRECEDENCE — apply this BEFORE the numbered rules below: a HARD\n"
        "CONTRADICTION always outranks every leniency or grounding rule. A\n"
        "claim whose named geography, stage, sector, or portfolio-cluster\n"
        "pattern CONFLICTS with the firm's verbatim evidence — the portfolio\n"
        "companies' actual regions / sectors / descriptions, or\n"
        "context.cluster_patterns[dim].dominant_pattern — is FABRICATED. This\n"
        "holds EVEN WHEN the corresponding structured field\n"
        "(firm_geography / firm_stages) is ABSENT or EMPTY, and EVEN WHEN a\n"
        "cluster pattern would otherwise ground a matching claim. The\n"
        "'prefer UNVERIFIABLE when unsure' guidance (rule 6c) and the\n"
        "'cluster pattern -> VERIFIED' guidance (rule 9) NEVER upgrade a\n"
        "contradicting claim to unverifiable or verified — absence of a\n"
        "structured field is not a free pass for a claim the evidence\n"
        "actively contradicts.\n\n"
        "Rules:\n"
        "1. Extract only FACTUAL claims (names, numbers, citations). Skip\n"
        "   pure opinion, recommendation phrasing, and generic advice.\n"
        "2. A timing claim (e.g. \"actively deploying\", \"3 investments in\n"
        "   90 days\") MUST be cross-checked against\n"
        "   context.timing_metrics. If the report says \"actively\n"
        "   deploying\" but timing_metrics.thesis_temperature is \"cold\",\n"
        "   that is FABRICATED.\n"
        "3. A ticket / check-size band claim uses the following logic:\n"
        "   a) A SPECIFIC invented number NOT grounded in any context field\n"
        "      (e.g. \"writes checks of up to $50M\") is FABRICATED.\n"
        "   b) A SPECIFIC range that appears in firm_check_size.minimum /\n"
        "      maximum / typical OR context.news_articles[].ticket_size is\n"
        "      VERIFIED.\n"
        "   c) A VAGUE inference explicitly hedged as inferred (e.g. \"likely\n"
        "      writes early-stage checks in the low single-digit millions\",\n"
        "      \"generally writes checks of a few million\") with NO\n"
        "      contradicting evidence is UNVERIFIABLE — not fabricated.\n"
        "      Use UNVERIFIABLE (not FABRICATED) when: the claim is hedged\n"
        "      (\"likely\", \"generally\", \"appears to\", \"probably\"), the\n"
        "      magnitude is vague (\"a few million\", \"low single-digit\n"
        "      millions\"), and the context has no check-size field at all.\n"
        "      Reserve FABRICATED for: a specific dollar/euro amount that\n"
        "      is NOT in the context OR that CONTRADICTS a present\n"
        "      firm_check_size field.\n"
        "4. A partner name claim MUST appear in context.partners[].name.\n"
        "   A claimed role (e.g. \"leads FinTech\") MUST match\n"
        "   context.partners[].role. A claimed sector focus (e.g. \"focuses\n"
        "   on Consumer\") MUST appear in context.partners[].focus_sectors\n"
        "   for that partner (when present). Mismatched role or sector is\n"
        "   FABRICATED.\n"
        "5. A portfolio company name MUST appear in\n"
        "   context.portfolio_companies[].name (case-insensitive,\n"
        "   trailing-period-insensitive).\n"
        "6. A firm geography or stage claim uses the following logic:\n"
        "   a) If context.firm_geography[] or context.firm_stages[] IS\n"
        "      PRESENT and the claim CONTRADICTS it — FABRICATED.\n"
        "   b) If context.firm_geography[] or context.firm_stages[] IS\n"
        "      PRESENT and the claim MATCHES it — VERIFIED.\n"
        "   c) If the relevant context field IS ABSENT OR EMPTY, judge the\n"
        "      claim against the OTHER evidence in the context, chiefly the\n"
        "      portfolio companies (their descriptions, sectors, names):\n"
        "      - A VAGUE or plausible inference that is CONSISTENT WITH the\n"
        "        portfolio evidence (e.g. the portfolio is food-tech\n"
        "        startups → \"invests in European food companies\", or\n"
        "        \"focuses on early-stage food startups\") is UNVERIFIABLE\n"
        "        (not fabricated). When unsure but the claim does not\n"
        "        conflict with anything, prefer UNVERIFIABLE.\n"
        "      - A SPECIFIC geography or stage that CONTRADICTS or is\n"
        "        clearly IMPLAUSIBLE given the portfolio evidence is\n"
        "        FABRICATED — even though the structured field is absent.\n"
        "        Examples: a portfolio of European/Western food-tech\n"
        "        startups paired with \"primarily invests in Southeast\n"
        "        Asian markets\" (the named region contradicts where the\n"
        "        portfolio actually operates) → FABRICATED; an early-stage\n"
        "        venture portfolio paired with \"exclusively backs\n"
        "        late-stage, pre-IPO buyout deals\" (the named stage\n"
        "        contradicts the portfolio's character) → FABRICATED.\n"
        "        Absence of the structured field is NOT a free pass: a\n"
        "        specific, evidence-contradicting region/stage is invented,\n"
        "        not merely unverifiable.\n"
        "   A recent-deal / investment reference (e.g. \"recently backed\n"
        "   Modern Relay\") may be grounded in context.recent_deals[] in\n"
        "   addition to news_articles[] and portfolio_companies[].\n"
        "7. The `evidence` field for verified claims MUST cite the\n"
        "   exact context-dict path that grounded it, e.g.\n"
        "   'portfolio_companies[].name=Acme',\n"
        "   'firm_check_size.news_derived_band', 'firm_geography=Europe',\n"
        "   or 'timing_metrics.thesis_temperature=hot'.\n"
        "8. A claim that names an entity as a \"backer\", \"investor\",\n"
        "   \"co-investor\", or \"LP\" in the firm MAY be grounded by\n"
        "   context.co_investors[].name. If the named entity appears in\n"
        "   context.co_investors[], the claim is VERIFIED. If\n"
        "   context.co_investors[] is empty or the name is absent, the\n"
        "   claim is UNVERIFIABLE (not automatically FABRICATED — the\n"
        "   co-investor list may simply be incomplete).\n"
        "   Deal-count sub-rule: if the claim states a specific number of\n"
        "   co-investments with the named entity (e.g. \"co-invested 4 times\"),\n"
        "   check context.co_investors[].deals for that entity. If `.deals`\n"
        "   is present AND matches the claimed count → VERIFIED. If `.deals`\n"
        "   is present AND contradicts the claimed count → FABRICATED. If\n"
        "   `.deals` is null or absent → UNVERIFIABLE (absence of a count\n"
        "   is not a contradiction).\n"
        "9. A portfolio \"clustering pattern\" claim — e.g. \"the firm's\n"
        "   portfolio shows a moderate clustering pattern for 'b2b_enterprise'\n"
        "   customer segments\", \"the portfolio leans toward enterprise_sales\",\n"
        "   \"a moderate pattern for platform business models\" — MAY be\n"
        "   grounded against context.cluster_patterns when that field is\n"
        "   present. For each dimension (business_model, gtm_approach,\n"
        "   customer_segment, subsector, stage_maturity), check whether the\n"
        "   claim's named pattern token (e.g. \"b2b_enterprise\",\n"
        "   \"enterprise_sales\", \"platform\") matches\n"
        "   context.cluster_patterns[dim].dominant_pattern. When it matches,\n"
        "   the claim is VERIFIED. When it CONTRADICTS the dominant_pattern —\n"
        "   e.g. the claim says the portfolio clusters around \"b2c_consumer\"\n"
        "   but context.cluster_patterns.customer_segment.dominant_pattern is\n"
        "   \"b2b_enterprise\" — the claim is FABRICATED (a wrong cluster token\n"
        "   is invented, not merely unverifiable). The severity adjective in\n"
        "   the narrative (\"moderate\", \"strong\", \"major\") is a narrative\n"
        "   characterisation of prevalence — do NOT flag a severity-adjective\n"
        "   mismatch as fabricated; treat it as UNVERIFIABLE at most. When\n"
        "   context.cluster_patterns is absent or the named dimension is not\n"
        "   present, the claim is UNVERIFIABLE (not FABRICATED).\n"
        "10. A claim asserting that a firm focuses on or targets a specific\n"
        "   sector or theme (e.g. \"the firm explicitly focuses on FoodTech\",\n"
        "   \"Impact is a core investment theme\") MAY be grounded by\n"
        "   context.firm_focus_areas[]. Matching logic:\n"
        "   a) If context.firm_focus_areas[] IS PRESENT AND NON-EMPTY and\n"
        "      the claimed sector/theme token appears in the list (case-\n"
        "      insensitive) — VERIFIED.\n"
        "   b) If context.firm_focus_areas[] IS PRESENT AND NON-EMPTY and\n"
        "      the claimed sector/theme is ABSENT from the list — FABRICATED.\n"
        "      A non-empty focus_areas list is authoritative: a sector absent\n"
        "      from a known list is contradicted by that list, not merely\n"
        "      unverifiable. Reserve FABRICATED for sectors that could not\n"
        "      plausibly derive from portfolio evidence either.\n"
        "   c) If context.firm_focus_areas[] IS EMPTY OR ABSENT, fall back\n"
        "      to portfolio evidence (rule 6 pattern) — the claim is\n"
        "      UNVERIFIABLE unless the portfolio evidence actively contradicts\n"
        "      it, in which case it is FABRICATED.\n"
        "   SCOPE: Rule 10 applies ONLY to sector and investment-theme focus\n"
        "   tokens (e.g. 'FoodTech', 'Impact', 'DeepTech', 'Clean Tech'). It\n"
        "   does NOT modify geographic, stage, or check-size/ticket-size\n"
        "   detection — those are governed exclusively by rules 3, 4, 6, and\n"
        "   7. Specifically:\n"
        "   - A geographic claim (e.g. 'primarily invests in Southeast Asia')\n"
        "     MUST be evaluated against the portfolio's actual geographies per\n"
        "     rule 6, regardless of whether firm_focus_areas is empty.\n"
        "   - A check-size / ticket-size claim (e.g. 'writes checks of around\n"
        "     $80M') MUST be evaluated per rule 3: a specific dollar amount not\n"
        "     in any context field is FABRICATED, even when hedged with\n"
        "     'probably' or 'around', if the amount is implausibly large (e.g.\n"
        "     $80M for a firm whose portfolio consists of early-stage food-tech\n"
        "     startups). Do NOT apply rule 10's 'UNVERIFIABLE' fallback to\n"
        "     check-size or geographic claims.\n\n"
        "Output JSON ONLY — no commentary outside the JSON. Schema:\n\n"
        "{\n"
        '  "audits": [\n'
        '    {\n'
        '      "claim_text": "verbatim snippet of the claim",\n'
        '      "claim_type": "portfolio_company|news_citation|partner|ticket_band|timing_signal|other",\n'
        '      "verdict": "verified|fabricated|unverifiable",\n'
        '      "evidence": "context path that grounds the claim, or null"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        "=== REPORT NARRATIVE ===\n"
        f"{report_text}\n\n"
        "=== CONTEXT DICT ===\n"
        f"{context_json}\n"
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _apply_redaction(
    report_text: str,
    audits: list,
    policy: RedactPolicy,
) -> str:
    """Rewrite the report per the chosen redaction policy.

    - ``strip``: replace the verbatim claim text with
      ``[claim removed: not in context]``.
    - ``annotate`` (default): append `` (fabricated)`` to ``fabricated``
      claims and `` (unverified)`` to ``unverifiable`` ones, so the consumer
      sees a caveat without losing the text.
    - ``pass_through``: no rewrite; the structured audit alone tells the
      story.
    """
    if not report_text or not audits or policy == "pass_through":
        return report_text or ""

    rewritten = report_text
    for audit in audits:
        verdict = audit.verdict if isinstance(audit, ClaimAudit) else audit.get("verdict")
        claim_text = (
            audit.claim_text if isinstance(audit, ClaimAudit) else audit.get("claim_text")
        )
        if not claim_text:
            continue
        if policy == "strip" and verdict == "fabricated":
            rewritten = _safe_replace(
                rewritten, claim_text, "[claim removed: not in context]"
            )
        elif policy == "annotate" and verdict in ("fabricated", "unverifiable"):
            tag = " (fabricated)" if verdict == "fabricated" else " (unverified)"
            rewritten = _safe_replace(
                rewritten, claim_text, f"{claim_text}{tag}"
            )
    return rewritten


def _safe_replace(haystack: str, needle: str, replacement: str) -> str:
    """Replace the first occurrence of ``needle`` in ``haystack``.

    Tolerates whitespace differences via a fallback to a relaxed-pattern
    regex search. Used by the redactor — never raises; returns
    ``haystack`` unchanged when the needle can't be found.
    """
    if not needle:
        return haystack
    # Fast path — exact match.
    idx = haystack.find(needle)
    if idx >= 0:
        return haystack[:idx] + replacement + haystack[idx + len(needle):]
    # Relaxed match — collapse internal whitespace runs and try regex.
    try:
        pattern = re.escape(needle.strip())
        # Allow any-whitespace where the source had exactly one.
        pattern = re.sub(r"\\\s+", r"\\s+", pattern)
        match = re.search(pattern, haystack)
        if match:
            return (
                haystack[: match.start()]
                + replacement
                + haystack[match.end():]
            )
    except re.error:
        pass
    return haystack


# ---------------------------------------------------------------------------
# Internal narrative softener
# ---------------------------------------------------------------------------


def _cleanup_after_removal(text: str) -> str:
    """Tidy *text* after a claim span was excised in place.

    - Collapse runs of HORIZONTAL whitespace (spaces/tabs) to a single
      space.  Newlines are preserved deliberately: collapsing ``\\n\\n``
      paragraph breaks would silently reflow unrelated verified paragraphs
      elsewhere in the field on any removal (python-reviewer MEDIUM).
    - Drop whitespace that now sits before sentence punctuation
      (``" ."`` → ``"."``) created when a span ended mid-sentence.
    - Strip leading orphan punctuation / whitespace left at the start
      (``". Foo"`` → ``"Foo"``).
    - Strip trailing orphan clause punctuation left at the end
      (``"They focus on SaaS,"`` → ``"They focus on SaaS"``).
    - Collapse doubled terminal punctuation (``"FinTech.. Seed."`` →
      ``"FinTech. Seed."``) created when a span ended right before an
      existing period.
    - Strip outer whitespace.

    Best-effort cosmetic only — never raises.
    """
    if not text:
        return text
    # Collapse horizontal whitespace runs only (preserve newlines / paragraph
    # breaks — see MEDIUM note above).
    out = re.sub(r"[ \t]{2,}", " ", text)
    # Remove horizontal whitespace that now precedes terminal/clause punctuation.
    # Use [ \t]+ rather than \s+ so newlines are NOT collapsed here either
    # (e.g. a verified paragraph ending "\n\n" before "." should survive).
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    # Collapse doubled terminal punctuation left when a span ended just
    # before an existing sentence terminator.
    out = re.sub(r"([.!?])\1+", r"\1", out)
    # Strip leading orphan punctuation + whitespace.
    out = re.sub(r"^[\s.,;:!?]+", "", out)
    # Strip trailing orphan clause punctuation (a dangling comma/semicolon/
    # colon left after a mid-sentence excision).
    out = re.sub(r"[,;:]\s*$", "", out)
    return out.strip()


def _strip_claim_span(text: str, claim: str) -> str:
    """Excise the *claim* span from *text* (lexical, whitespace-tolerant).

    Built on :func:`_safe_replace` (already whitespace-tolerant + regex
    fallback) — replaces the matched span with ``""`` then tidies the
    surrounding punctuation/whitespace.  Unlike sentence-splitting this
    NEVER touches text outside the matched span, so:

    - abbreviation periods inside the claim (``U.S.``, ``Inc.``) don't
      mis-segment — the full claim span is matched verbatim;
    - quoted / parenthesised terminal punctuation in the claim doesn't
      bleed into the following sentence;
    - a short claim (a bare name) removes only its own characters, leaving
      the rest of the surrounding sentence — and any verified content in
      it — intact.

    Returns *text* unchanged when *claim* is empty or not found (lexical
    match only — a semantic paraphrase that doesn't substring-match is
    left in place; fail-safe per the integrity-gate paraphrase caveat).
    """
    if not claim or not text:
        return text
    stripped = _safe_replace(text, claim, "")
    if stripped == text:
        return text  # claim not found — no change, skip cleanup
    return _cleanup_after_removal(stripped)


def _soften_fabricated_clauses(
    narrative: dict,
    audit_result_dict: Optional[dict],
) -> dict:
    """Silently soften / strip ONLY ``fabricated`` claims from the narrative.

    Called by the report engine when the claim
    auditor ran and produced verdicts.  Leaves ``verified`` / ``unverifiable``
    claims **completely untouched**.  For string fields that become fully
    empty after removal (100% fabricated), a neutral note
    ``"Withheld — insufficient grounded evidence."`` is substituted and a
    ``WARNING`` is logged — the old "keep original" behaviour was removed
    when the downstream integrity gate was dropped.  List fields drop
    entirely-fabricated bullets but keep partial remainders.  Adds NO
    per-claim pill markup — no ``(fabricated)`` / ``✗`` annotations are
    inserted.

    Removal is **lexical span removal** keyed on each fabricated claim's
    verbatim text (``_strip_claim_span`` → ``_safe_replace``).  This is the
    robust primitive vs. sentence-splitting: it never touches text outside
    the matched span (no abbreviation / quoted-punctuation mis-splits) and
    a short claim removes only its own characters (verified content in the
    same sentence/bullet survives).  A semantic paraphrase that the auditor
    flagged but which does not substring-match is left in place (fail-safe).

    For **string fields** (``summary``, ``recommendation``): each fabricated
    span is excised and whitespace tidied.  If the result is empty /
    whitespace-only (100% fabricated), the field is replaced with a brief
    neutral note (``"Withheld — insufficient grounded evidence."``).  A
    ``WARNING`` is logged so the event is observable.  The old "keep
    original" behaviour was the right fail-safe when a downstream integrity
    gate existed; that gate was removed, so restoring fabricated
    text is now the worse failure.

    For **list fields** (``strengths``, ``concerns``, ``red_flags``,
    ``next_steps``, ``how_to_approach``, ``cited_evidence``,
    ``portfolio_pattern_analysis``, ``score_deep_dive``,
    ``data_limitations``): each bullet has fabricated spans excised; a
    bullet that becomes empty is dropped (it was entirely fabricated), a
    bullet with a non-empty remainder is kept.  Non-string items (dicts,
    nested structures) are preserved as-is.

    Demo-safety: when *audit_result_dict* is ``None`` (audit was off /
    short-circuited) the function returns *narrative* unchanged via the
    ``if not audit_result_dict: return narrative`` fast path.

    Parameters
    ----------
    narrative:
        The narrative dict assembled by the engine.  Never mutated.
    audit_result_dict:
        The audit dict produced by the report engine (keys: audits, verified,
        fabricated, unverifiable, …) OR ``None`` when the audit did not run.

    Returns
    -------
    dict
        A new dict identical to *narrative* except that fabricated-claim
        spans / bullets have been silently removed.
    """
    # Fast path — no audit ran: return unchanged (demo-safety guard).
    if not audit_result_dict:
        return narrative

    audits_raw = audit_result_dict.get("audits") or []
    fabricated_claim_texts: list = [
        (a.get("claim_text") or "").strip()
        for a in audits_raw
        if isinstance(a, dict) and a.get("verdict") == "fabricated"
    ]
    # Drop empty strings defensively.
    fabricated_claim_texts = [t for t in fabricated_claim_texts if t]

    if not fabricated_claim_texts:
        # No fabricated claims — return original unchanged.
        return narrative

    # --- String fields: span removal + blank-guard ---
    _STRING_FIELDS = ("summary", "recommendation")
    # --- List fields: per-bullet span removal, drop-if-empty ---
    _LIST_FIELDS = (
        "strengths",
        "concerns",
        "red_flags",
        "next_steps",
        "how_to_approach",
        "cited_evidence",
        "portfolio_pattern_analysis",
        "score_deep_dive",
        "data_limitations",
    )

    out = dict(narrative)  # shallow copy — never mutate input

    for field_name in _STRING_FIELDS:
        original = out.get(field_name)
        if not isinstance(original, str) or not original:
            continue
        text = original
        for claim in fabricated_claim_texts:
            text = _strip_claim_span(text, claim)
        # Blank-guard: if removal emptied the field (100% fabricated), emit
        # a field-neutral note instead of restoring fabricated text verbatim.
        # The old "keep original" rule was the right fail-safe when a partial
        # integrity gate existed to neutralize the field downstream; that gate
        # was removed, so restoring fabricated content here is now
        # load-bearing bad UX — the founder would read contradicted text.
        if text.strip():
            out[field_name] = text
        else:
            logger.warning(
                "_soften_fabricated_clauses: blank-guard fired on %r — "
                "field was 100%% fabricated; replacing with neutral note.",
                field_name,
            )
            out[field_name] = "Withheld — insufficient grounded evidence."

    for field_name in _LIST_FIELDS:
        items = out.get(field_name)
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if not isinstance(item, str):
                # Non-string items (dicts, nested structures) are preserved —
                # we can't text-match them safely.
                kept.append(item)
                continue
            stripped = item
            for claim in fabricated_claim_texts:
                stripped = _strip_claim_span(stripped, claim)
            if stripped.strip():
                # Remainder survives — keep the softened bullet.
                kept.append(stripped)
            # else: bullet was entirely fabricated → drop it.
        out[field_name] = kept

    return out


# ---------------------------------------------------------------------------
# Cost estimator — pure math, no Gemini import.
# ---------------------------------------------------------------------------


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        (input_tokens / 1_000_000.0) * _GEMINI_FLASH_INPUT_PER_1M
        + (output_tokens / 1_000_000.0) * _GEMINI_FLASH_OUTPUT_PER_1M
    )


# ---------------------------------------------------------------------------
# Verdict parsing — defensive JSON shape coercion.
# ---------------------------------------------------------------------------

_VALID_VERDICTS: tuple = ("verified", "fabricated", "unverifiable")
_VALID_TYPES: tuple = (
    "portfolio_company",
    "news_citation",
    "partner",
    "ticket_band",
    "timing_signal",
    "other",
)

# Schema-constrain the audit call so
# Gemini pins the output shape (mirrors the JSON spec in ``_build_audit_prompt``).
# Plain-dict literal so the module stays CI-strip safe (no google.genai import
# at module load). ``_parse_audits_payload`` still tolerates extra/unknown
# values, so this is a belt-and-braces backstop, not the sole parse defence.
AUDIT_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "audits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {"type": "string"},
                    "claim_type": {"type": "string"},
                    "verdict": {"type": "string"},
                    "evidence": {"type": "string", "nullable": True},
                },
                "required": ["claim_text", "verdict"],
            },
        }
    },
    "required": ["audits"],
}


def _parse_audits_payload(raw_text: str) -> list:
    """Parse the LLM JSON payload into a list of :class:`ClaimAudit`.

    Tolerates Gemini code-fence wrappers (```json ... ```), unknown
    verdict values (coerced to ``unverifiable``), and partial dicts.
    Returns ``[]`` when the payload is unparseable.
    """
    if not raw_text:
        return []
    text = raw_text.strip()
    # Strip ```json fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("_parse_audits_payload: JSONDecodeError — audit zeroed: %.200s", raw_text)
        return []

    if not isinstance(payload, dict):
        return []
    raw_audits = payload.get("audits", [])
    if not isinstance(raw_audits, list):
        return []

    out: list = []
    for entry in raw_audits:
        if not isinstance(entry, dict):
            continue
        claim_text = (entry.get("claim_text") or "").strip()
        if not claim_text:
            continue
        claim_type = entry.get("claim_type") or "other"
        if claim_type not in _VALID_TYPES:
            claim_type = "other"
        verdict = entry.get("verdict") or "unverifiable"
        if verdict not in _VALID_VERDICTS:
            verdict = "unverifiable"
        evidence = entry.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            evidence = str(evidence)
        out.append(
            ClaimAudit(
                claim_text=claim_text,
                claim_type=claim_type,
                verdict=verdict,
                evidence=evidence,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def audit_report_claims(
    report_text: str,
    context: dict,
    gemini_client: _GeminiClientProtocol,
    *,
    model: Optional[str] = None,
    cost_log_callback: Optional[Callable[[float, int, int], None]] = None,
) -> AuditResult:
    """Audit every factual claim in ``report_text`` against ``context``.

    Single Gemini Flash call: extracts every claim AND verifies it in
    one round-trip. Returns a :class:`AuditResult` with per-claim
    audits + a redacted report (per ``RCF_REDACT_POLICY`` env var).

    Args:
        report_text: The narrative prose generated by the engine. Can
            be the full markdown report OR just the LLM-generated
            narrative (whichever the caller wants audited).
        context: The same dict the engine populates and threads to
            ``_generate_narrative`` (sectors/stages/geography +
            portfolio_companies + news_articles + partners +
            knowledge_paths + timing_metrics + similar_firms +
            thesis_clusters). Missing keys are tolerated; the auditor
            simply has less evidence to verify against.
        gemini_client: A live ``google.genai.Client`` instance (or any
            object exposing ``client.models.generate_content``).
            Injected so the helper stays CI-strip safe — no SDK
            import at module load.
        model: Gemini model id. Defaults to ``GEMINI_MATCHING_MODEL``
            (resolved lazily from ``pierre.config`` — ``pierre.config`` is
            pure stdlib so the lazy import stays CI-strip safe; we use
            the matching model constant (one chokepoint) so
            a future model swap edits a single config line and every
            auditor call site moves with it).
        cost_log_callback: Optional callable invoked with
            ``(cost_usd, input_tokens, output_tokens)`` for downstream
            telemetry. Failures inside the callback are swallowed.

    Returns:
        :class:`AuditResult`. When the helper short-circuits
        (empty report, empty context, Gemini call raises), returns a
        zero-count result with ``redacted_report=report_text`` so the
        caller can chain unconditionally.
    """
    if not report_text:
        return AuditResult(
            verified_count=0,
            fabricated_count=0,
            unverifiable_count=0,
            audits=[],
            redacted_report="",
            budget_truncated=False,
            audit_cost_usd=0.0,
        )

    truncated_text, was_truncated = _truncate_report(report_text)
    context_summary = _summarize_context_for_prompt(context or {})

    # When the context is completely empty there is nothing to verify
    # against — every claim would land as ``fabricated`` or
    # ``unverifiable``, which is noise. Short-circuit with a
    # pass-through audit so the caller never blocks on an empty-context
    # report.
    if _is_context_empty(context_summary):
        logger.warning(
            "audit_report_claims: empty context dict — skipping audit. "
            "The narrative was generated without verifiable evidence; "
            "consider whether the report path is correctly assembling "
            "portfolio_companies / news_articles / partners before "
            "generate_report runs."
        )
        return AuditResult(
            verified_count=0,
            fabricated_count=0,
            unverifiable_count=0,
            audits=[],
            redacted_report=report_text,
            budget_truncated=was_truncated,
            audit_cost_usd=0.0,
        )

    prompt = _build_audit_prompt(truncated_text, context_summary)

    # Resolve model lazily — pierre.config is pure stdlib so it's
    # safe to import here, but we keep it out of module top (zero project
    # imports up there). Going through the per-use-case constant keeps
    # model choice in one place; the matching model is correct for the
    # audit since it is the same Gemini Flash family that produced the
    # narrative.
    if model is None:
        from pierre.config import GEMINI_MATCHING_MODEL as _resolved_model
        model = _resolved_model

    response = None
    raw_text = ""
    audit_cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    try:
        # google.genai.types is imported lazily here so the helper module
        # itself stays import-safe in CI-strip. Caller already proved the
        # SDK is installed by passing a live client.
        from google.genai import types as _genai_types  # type: ignore

        # Schema-constrain the call (``response_schema=AUDIT_RESPONSE_SCHEMA``)
        # and add exponential backoff on transient API errors via the shared
        # ``call_with_retry`` chokepoint. ``_llm_retry`` is pure-stdlib so it
        # is safe to import here. The auditor already degrades
        # gracefully (``_parse_audits_payload`` returns ``[]`` on bad JSON and
        # the ``except`` below pass-throughs), so a bounded JSON retry is
        # unnecessary — ``parse_attempts=1`` with an identity parse keeps the
        # backoff on the API call only.
        from pierre.agents._llm_retry import call_with_retry as _call_with_retry

        config_kwargs = dict(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=AUDIT_RESPONSE_SCHEMA,
        )
        response = _call_with_retry(
            lambda: gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(**config_kwargs),
            ),
            lambda r: r,
            parse_attempts=1,
            label="claim_audit",
        )
        raw_text = getattr(response, "text", "") or ""

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            audit_cost_usd = _estimate_cost_usd(input_tokens, output_tokens)
    except Exception:
        # Best-effort: a Gemini failure must not corrupt the report path.
        # Log loudly so the operator sees the
        # auditor went dark — but return a pass-through result so the
        # narrative still ships.
        logger.warning(
            "audit_report_claims: Gemini call raised — passing report "
            "through without audit; "
            "if it persists, check API key / model availability.",
            exc_info=True,
        )
        return AuditResult(
            verified_count=0,
            fabricated_count=0,
            unverifiable_count=0,
            audits=[],
            redacted_report=report_text,
            budget_truncated=was_truncated,
            audit_cost_usd=0.0,
        )

    audits = _parse_audits_payload(raw_text)

    verified = sum(1 for a in audits if a.verdict == "verified")
    fabricated = sum(1 for a in audits if a.verdict == "fabricated")
    unverifiable = sum(1 for a in audits if a.verdict == "unverifiable")

    policy = _resolve_redact_policy()
    redacted = _apply_redaction(report_text, audits, policy)

    if cost_log_callback is not None:
        try:
            cost_log_callback(audit_cost_usd, input_tokens, output_tokens)
        except Exception:
            logger.debug(
                "audit_report_claims: cost_log_callback raised — ignoring",
                exc_info=True,
            )

    return AuditResult(
        verified_count=verified,
        fabricated_count=fabricated,
        unverifiable_count=unverifiable,
        audits=audits,
        redacted_report=redacted,
        budget_truncated=was_truncated,
        audit_cost_usd=audit_cost_usd,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_context_empty(summary: dict) -> bool:
    """True when the verification corpus has no usable evidence at all."""
    return not (
        summary.get("portfolio_companies")
        or summary.get("news_articles")
        or summary.get("partners")
        or summary.get("co_investors")
        or summary.get("timing_metrics")
        or summary.get("similar_firms")
        or summary.get("thesis_clusters")
        or summary.get("cluster_patterns")
    )


def report_grounding_band(audit: Optional[dict]) -> Optional[str]:
    """Aggregate an auditor result into the shared high/medium/low band.

    Band-counting policy B: surface ONE grounding-level vocabulary across chat
    answers and deep reports by feeding the auditor's per-claim verdict counts
    into the SAME ``groundedness_to_band`` the source system's chat assistant
    uses (reused from ``pierre.utils.grounding`` — not forked).

    **Policy B — fabrication-focused (the recommended/default counting):**

        supported = verified
        total     = verified + fabricated      # exclude `unverifiable`

    ``unverifiable`` (fair inference) is deliberately EXCLUDED from the
    denominator so the band means *"of the checkable claims, how many are
    invented vs grounded"* — fair inference doesn't drag the band down, which
    matches the softening semantics (we strip ONLY ``fabricated``). The chat
    assistant's exact band vocabulary + cutoffs (1.0 → high, ≥0.5 → medium, else low) are
    kept; only the numerator is fabrication-focused.

    Args:
        audit: the engine's audit result dict (keys ``verified`` /
            ``fabricated`` / ``unverifiable`` — the shape the source system's
            report generator produces), OR ``None`` when the
            audit did not run.

    Returns:
        ``"high" | "medium" | "low"``, or ``None`` when no band can be derived:

        * the audit did not run (``audit`` is falsy) — never render a report as
          ``low`` just because the auditor was off / errored;
        * every checkable claim is excluded, i.e. ``verified + fabricated == 0``
          (only ``unverifiable`` claims, or no claims at all) — there is nothing
          to score, so ``groundedness_to_band`` returns ``None`` via ``total<=0``.
    """
    # Explicit short-circuit: a present-but-empty / absent audit must NOT slip
    # through to a band. (groundedness_to_band(0, 0) already returns None, but
    # guarding here keeps the "audit didn't run → None" contract obvious.)
    if not audit:
        return None
    from pierre.utils.grounding import groundedness_to_band

    verified = int(audit.get("verified") or 0)
    fabricated = int(audit.get("fabricated") or 0)
    return groundedness_to_band(supported=verified, total=verified + fabricated)


def _resolve_redact_policy() -> RedactPolicy:
    """Read ``RCF_REDACT_POLICY`` from the env, defaulting to ``annotate``.

    Pure stdlib — env reads are safe at call-time (not at import-time)
    so the module remains testable without monkey-patching ``os.environ``.
    """
    raw = (os.environ.get("RCF_REDACT_POLICY") or "annotate").strip().lower()
    if raw in ("strip", "annotate", "pass_through"):
        return raw  # type: ignore[return-value]
    logger.warning(
        "audit_report_claims: unknown RCF_REDACT_POLICY=%r — falling back to "
        "'annotate'. Valid values: strip / annotate / pass_through.",
        raw,
    )
    return "annotate"
