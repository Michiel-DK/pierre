# Claim-auditor calibration report

> **Auto-generated** by `scripts/analysis/claim_auditor_calibration_harness.py`. Do not hand-edit — re-run the harness (`--render-md` to refresh from the committed snapshot without Gemini, or a full capture to re-measure).
>
> Snapshot captured: **2026-07-17** with `gemini-2.5-flash`

## What this measures

Our deep-report engine fact-checks every claim it writes against the structured evidence it was given, and labels each claim `verified` / `unverifiable` / `fabricated`. By default a `fabricated` claim is tagged `(fabricated)` and an `unverifiable` one `(unverified)` in the text; a stricter setting removes fabricated claims. This harness runs a **fixed, hand-labeled set of test claims** through that auditor and scores how often it gets them right — the **groundedness calibration** of the engine.

The headline number is the **fabricated false-positive rate**: of the claims that are genuinely supported or fairly inferable (and so should survive), how many did the auditor wrongly flag as fabricated? Lower is better. Its counterweight is **recall**: of the genuinely fabricated claims, how many did it catch?

## Headline result

| Metric | Result | Target | Meaning |
|---|---|---|---|
| **Fabricated false-positive rate** | **0.0%** | ≤ 15.0% | share of legitimate claims wrongly flagged fabricated |
| **Fabricated recall** | **100.0%** | ≥ 80.0% | share of real fabrications correctly caught |
| Test claims | 57 | — | hand-labeled fixtures |

> **Read this as a baseline, not a green-light.** The target threshold is an internal bar, not an external standard, and a passing number here is the *precondition* for trusting the auditor in production — not proof it is fully calibrated. The defects below are the retune list.
>
> **Sample size.** 38 legitimate and 19 fabricated claims, written and labeled by the author. Zero errors on samples this small still allow a true false-positive rate up to ~8% and a true recall down to ~84% (95%, rule of three).

## Confusion matrix

Rows = the hand-assigned ground-truth label; columns = what the auditor actually decided. The diagonal-ish ideal: `verified`→verified, `fair_inference`→unverifiable (or verified — both survive), `fabricated`→fabricated.

| Ground truth ↓ / Auditor → | verified | unverifiable | fabricated | no-claim |
|---|---|---|---|---|
| **verified** (20) | 20 | 0 | 0 | 0 |
| **fair_inference** (18) | 7 | 11 | 0 | 0 |
| **fabricated** (19) | 0 | 0 | 19 | 0 |

Per-verdict precision / recall:

| Verdict | Precision | Recall |
|---|---|---|
| verified | 0.741 | 1.000 |
| unverifiable | 1.000 | 0.611 |
| fabricated | 1.000 | 1.000 |

## Logged defects (the retune list)

_None on this run._

## How to reproduce

```bash
# Full re-measure (needs a Gemini key, no DB):
python scripts/analysis/claim_auditor_calibration_harness.py
# Gate before shipping an auditor change (needs a Gemini key; exits 1 on regression):
python scripts/analysis/claim_auditor_calibration_harness.py --check
# Refresh THIS doc from the committed snapshot (no Gemini):
python scripts/analysis/claim_auditor_calibration_harness.py --render-md
```

