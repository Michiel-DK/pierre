# pierre

The evaluation layer of a VC-matching system I built: it matches a startup pitch to European VC firms and writes an
LLM-generated report on each firm. The full system is private; this repo holds the parts that test whether its own
output can be trusted, plus a small ranking demo that runs on its own.

There are no real-world match outcomes behind any of this (the system has close to zero resolved matches). What it
can show is calibration: how often the fact-checker is right, and whether the matcher's thresholds ever fire.

## What the measurements found

**1. The fact-checker on the reports is accurate on a small labelled set.**
Every report goes through a claim auditor (`pierre/agents/_claim_auditor.py`): one Gemini Flash call extracts each
factual claim and checks it against the evidence the report was built from, labelling it `verified`,
`unverifiable` or `fabricated`. Run over 57 hand-labelled claims, it wrongly flagged 0% of legitimate claims
(target ≤ 15%) and caught 100% of fabricated ones (target ≥ 80%).
The set is small and I wrote it myself: 38 legitimate and 19 fabricated claims. Zero errors at that size still allows
a true false-positive rate up to ~8% and a true recall down to ~84%.
→ [report](docs/calibration/claim_auditor_calibration_report.md)

**2. The competitor warning never fired.**
The matcher was meant to flag a VC firm that had already backed one of the founder's competitors (a -20% penalty and
a "direct competitor" line in the report). It did that when the embedding similarity between the pitch and one of
the firm's portfolio companies reached 0.80. Across 170,088 real pitch↔company comparisons the highest similarity
was 0.733, so the warning could never trigger. The threshold had been tuned on company↔company pairs, which score
higher than a short pitch against a company description. Two related thresholds had the same problem.
→ [report](docs/calibration/threshold_reachability_report.md)

**3. No threshold could fix it.**
Against unrelated companies, a threshold of 0.608 looks fine: 18 of 20 labelled competitor pairs caught, no false
positives. That set is small: two test cases, and the 20 pairs are about 7 distinct companies counted once per investor.
The decisive test replays the real matcher on 6 test pitches and counts how many of the firms it had already
retrieved get flagged as conflicts. At 0.62, 45–95% of them are flagged; at 0.65, still 20–60%. Those firms invest
in the same sector, so they score in the same band as real competitors (0.57–0.67 in the labelled set). Raise the
bar to 0.68 and flagging drops to 0–35%, but no labelled competitor scores that high either. The harness's
`--check` exits 1 on purpose: that is the finding. In the private system the threshold was replaced by an LLM check
on the shortlisted pairs; that code is not in this repo.
→ [report](docs/calibration/competitor_threshold_calibration_report.md)

## Demo: rank your own company list

`pierre/byodata` takes a CSV or Excel file of companies and a paragraph describing what you are looking for, and
returns the list best-fit first. Every row is embedded and only the top 50 by cosine similarity are graded by the LLM
(in two batched calls), so a 5,000-row file costs the same as a 60-row one. Nothing is stored.

```
$ make demo
Embedder: offline hashing (lexical, deterministic)
Rows: 20 in file, 19 ranked, 1 skipped (too little description), 0 truncated

 #  similarity  grade  name — sector, stage, geo
 1      0.4000         Corvillex Grid — climate, Pre-seed, Spain
 2      0.3441         Umbrevia Bio — climate, Seed, Sweden
 3      0.3441         Bexmirra Materials — climate, Seed, UK
 4      0.2236         Yarvindel Clinics — healthtech, Seed, Italy
 ...
```

Without `GOOGLE_API_KEY` it uses a word-overlap embedder so it runs offline; that ranking is lexical, not semantic
(hence a healthtech company at #4). With a key it uses Gemini embeddings plus the LLM grade. The sample companies
are made up.

## Run it

Python 3.11+.

```bash
make install   # venv + package + test deps
make test      # unit tests: no network, no database, no API key
make demo      # the ranking demo above
make reports   # re-render the three reports from their committed snapshots; fails if any changes
```

The claim-auditor result can be re-measured with a Gemini key
(`python scripts/analysis/claim_auditor_calibration_harness.py`). The two threshold reports were measured on the
private system's database; this repo keeps the snapshots, re-renders them and tests the verdict logic, but cannot
re-measure them.

## Layout

```
pierre/agents/     claim auditor, its calibration fixtures, LLM retry helper
pierre/matching/   competitor-threshold calibration logic, LLM reranker
pierre/byodata/    the bring-your-own-list ranker
pierre/utils/      grounding checks, LLM cost tracking
scripts/analysis/  the three harnesses / renderers
tests/             unit tests and golden snapshots
docs/calibration/  the three generated reports
```

MIT licence.
