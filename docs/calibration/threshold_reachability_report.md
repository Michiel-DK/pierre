# Threshold reachability report

> Measured by the source system's reachability harness on its private corpus; that harness needs the original database and is not part of this repo. The committed snapshot (`tests/golden/threshold_reachability_snapshot.json`) is the record. Re-render with `scripts/analysis/render_threshold_reachability.py`; do not hand-edit.
>
> Snapshot captured: **2026-07-18T11:47:59.272541+00:00**

## What this measures

For each threshold constant, the **production comparison** — same task pairing, same text shapes the real call path uses — run over the real local corpus. `rows >=` counts how many real comparisons actually cross the bar; `observed max` is the highest value seen. **FAIL** = zero rows ever cross it (dead code that looks healthy — a false-negative machine in production). **WARN** = at least one row crosses, but the observed max sits within ~0.02 of the threshold (barely reachable — one embedding refresh from dead).

## Results

| Threshold | Value | Rows ≥ threshold | Observed max | Verdict | Comparison |
|---|---|---|---|---|---|
| `PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD` | 0.80 | 0 / 170088 | 0.7332 | 🔴 FAIL | 6 golden pitch descriptions x pgv_portfolio_companies.embedding (QA-clean, corpus-wide) |
| `PORTFOLIO_SEMANTIC_ADJACENT_THRESHOLD` | 0.75 | 0 / 170088 | 0.7332 | 🔴 FAIL | 6 golden pitch descriptions x pgv_portfolio_companies.embedding (QA-clean, corpus-wide) |
| `_COMPETITIVE_SIM_THRESHOLD` | 0.78 | 0 / 8160 | 0.7502 | 🔴 FAIL | 6 golden pitch descriptions x pgv_firm_thesis_clusters.centroid (uses production's strict '>' operator, not '>=') |
| `M14_EMBEDDING_CLUSTER_THRESHOLD` | 0.85 | 6859 / 27305 | 1.0000 | 🟢 PASS | all intra-firm article pairs, pgv_news_articles.embedding (company_mentioned guard applied) |
| `_CADENCE_COVERAGE_FLOOR` | 0.50 | 184 / 862 | 1.0000 | 🟢 PASS | per-firm dated/total ratio, all firms with >=1 QA-clean portfolio row |

## Dead thresholds (FAIL)

- **`PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD`** (0.80): 0 of 170088 rows cross it; observed max 0.7332. Source-system config — drives the -20% penalty + 'direct competitor' narrative.
- **`PORTFOLIO_SEMANTIC_ADJACENT_THRESHOLD`** (0.75): 0 of 170088 rows cross it; observed max 0.7332. Source-system config — informational 'thematically adjacent' tier, no penalty.
- **`_COMPETITIVE_SIM_THRESHOLD`** (0.78): 0 of 8160 rows cross it; observed max 0.7502. Function-local constant in the source system's query layer.

## How to reproduce

Re-measuring needs the original database and is not possible from this repo. The committed snapshot is the record; the verdict logic is tested without a database:

```bash
python -m pytest tests/unit/test_threshold_reachability_helpers.py
```
