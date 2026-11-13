# Claim auditor on agent-built contexts: 12-claim spot check

> **Auto-generated** by `scripts/analysis/agent_context_spot_check.py --render-md` from `tests/golden/agent_context_spot_check_snapshot.json`. Do not hand-edit.
> Snapshot captured: **2026-11-13** with `claude-opus-5-5`.

The 57-claim calibration (`claim_auditor_calibration_report.md`) measures the auditor on single-firm report contexts. The agent gives it a context built from tool results, which is a different distribution. This spot check runs 12 claims over such contexts: 6 true by the planted data and 6 false by it. The labels come from `data/agent/firms.json`, not from a model. It is reported beside the 57-claim number and is not merged with it.

| | claims | `verified` | `unverifiable` | `fabricated` | no claim extracted |
|---|---|---|---|---|---|
| true by the data | 6 | 6 | 0 | 0 | 0 |
| false by the data | 6 | 0 | 0 | 6 | 0 |

True claims flagged `fabricated`: **0 of 6**. False claims caught as `fabricated`: **6 of 6**.

Twelve claims cannot bound a rate. The counts show whether the auditor works on this kind of context at all; they are not a calibrated error rate, and they do not replace the 57-claim bound.

| id | label | effective verdict | claim |
|---|---|---|---|
| t1_hq_cheque | true | verified | Quillmoor Capital is headquartered in London and writes cheques between EUR 500,000 and EUR 3,000,000. |
| t2_portfolio_count | true | verified | Quillmoor Capital has six portfolio companies on record. |
| t3_shared_stage | true | verified | Brask Ventures and Gravick Partners both invest at Series A. |
| t4_2024_companies | true | verified | Merrowfield Ventures invested in Umbrevia Bio and Bexmirra Materials in 2024. |
| t5_shortlist_added | true | verified | Brask Ventures has been added to the shortlist. |
| t6_error_reported | true | verified | The portfolio statistics call for Ferrin Lane Capital failed, so no statistics are available. |
| f1_wrong_hq | false | fabricated | Quillmoor Capital is headquartered in Paris. |
| f2_wrong_founded | false | fabricated | Quillmoor Capital was founded in 2009. |
| f3_wrong_cheque | false | fabricated | Gravick Partners writes cheques of up to EUR 40 million. |
| f4_wrong_company_year | false | fabricated | Merrowfield Ventures invested in Saltmere Energy in 2024. |
| f5_wrong_fit | false | fabricated | A EUR 4,000,000 to EUR 5,000,000 raise falls inside Quillmoor Capital's cheque range. |
| f6_wrong_shortlist | false | fabricated | Orrindale Fund has been added to the shortlist. |

Re-run with a key: `python scripts/analysis/agent_context_spot_check.py`.
