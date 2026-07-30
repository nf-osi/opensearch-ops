# Automated search-relevance tuning

`tune.py` is a standalone harness that **recommends a better query-time search config** for a
SearchIndex by experimenting against its golden relevance set as a simpler, query-log-free
analog of OpenSearch's Search Relevance agent (useful when we don't have access to query logs or OpenSearch backend). 
The golden set is the objective function so no UBI / click data is needed.

It tunes **query-time knobs only**: query type, which fields to search, per-field boosts,
fuzziness, `tie_breaker`, `minimum_should_match`, phrase boosting. Live
index, analyzers, or `SearchConfiguration` are out-of-scope (that's a separate, privileged rebuild loop via
`config/apply_config.py`). Everything runs against the live index read-only and
anonymously, so it works on any public SearchIndex.

## Prerequisites

- A benchmark table at `benchmark/<table>/` with a `golden.yaml` (see the `goldie` agent —
  `.claude/skills/goldie/`). An explicit `fields.yaml` (today's hand-tuned strategy, if any) is
  optional — the per-field probe can't run over a bare `*`, so if no `fields.yaml` exists,
  `load_table()` bootstraps a starting `{field: boost}` map by profiling the live index
  (names/categories/free text; identifiers excluded) instead of requiring one be provided.
- Nothing else: no Anthropic API credentials are involved anywhere in this harness (see
  "Driving the agentic step").

## Usage

### Driving the agentic step

The "agent proposes candidates" step (below) is driven by the *calling* agent —
`sciops/agents/tuner/` (the Managed Agent behind the Slack bot), or Claude Code running the
`tuner` skill locally. No API key touches this harness at all: the caller — already an LLM,
running under the platform's own credentials — reads `<out>/<table>/round_context.json`
(profile, leaderboard, diagnostics, and the exact candidate JSON schema), authors
`candidates.json` itself, and drives the rounds:

```bash
python3 .claude/skills/tuner/scripts/tuning/tune.py init tools --objective ndcg
# agent reads tuner-runs/tools/round_context.json, writes candidates.json matching
# its "candidate_schema" key
python3 .claude/skills/tuner/scripts/tuning/tune.py add-candidates tools candidates.json
# repeat add-candidates for more rounds (round_context.json refreshes each time, including
# a stale_rounds counter + recommendation) — up to MAX_ROUNDS=8; add-candidates refuses a
# round past that (finalize instead), and truncates any batch over
# MAX_CANDIDATES_PER_ROUND=10 candidates rather than rejecting it outright. Then:
python3 .claude/skills/tuner/scripts/tuning/tune.py finalize tools
```

`init` options: `--objective {ndcg|mrr|recall|hit1|hitk}` (default `ndcg`), `--k`,
`--max-cases N` (smoke test on a slice), `--workers` (default 8), `--poll`. All three
subcommands take `--bench DIR` / `--out DIR`; see "Where artifacts go" below.

**Query volume, cost, and how long it takes.** Runtime is dominated almost entirely by live
query latency — every score is an async Synapse job (~1–3s each), run `--workers` at a time.
Cost therefore tracks the *number of live queries*, which is predictable: the field probe is
`cases × fields` queries (one-time, then cached to `<out>/<table>/probe.json`), plus a
live confirm (`cases` queries) per candidate, plus the final full-set verification (`2 × cases`).
The expensive variable is how many **non-decomposable** candidates a round contains (see the
next section) — each adds up to `LIVE_EVAL_BUDGET × cases` queries. Rough, observed ballparks
for `nf-tools` (48 cases, ~15 fields), ±50% given Synapse's variable job queue:

| run | expect |
| --- | --- |
| smoke test (`--max-cases 6`, decomposable candidates) | a few minutes |
| full 48-case run, decomposable candidates only, 2–3 rounds | ~20–40 min |
| each non-decomposable candidate added | ~+5–15 min |
| full-set finalize verification | ~3–8 min |

**Checkpointing / resumability.** Every phase that spends live queries is resumable, so a kill,
a timeout, or a crash costs the last few queries rather than the run:

| phase | checkpoint | on re-run |
| --- | --- | --- |
| probe (`init`) | `probe.json`, atomically, every 25 (case × field) pairs | skips cached pairs; prints `resuming probe: N/M` |
| seeds (`init`) | `state.json` after each seed and each optimized seed | prints `(cached)` per seed, re-queries none |
| candidates (`add-candidates`) | `state.json` after each candidate | continues from the persisted board |

`init` runs the probe *first*, before any seed, because it's the single most expensive step and
the one that caches — and because an unsearchable field then fails on query 1 rather than after
seven seeds' worth of live queries.

Re-running `init` resumes automatically when the setup is unchanged: the table, field list,
objective, `k`, and the exact case set are fingerprinted into `state.json`'s `init_key`, and a
mismatch starts clean rather than mixing scores from two different experiments. `--fresh` forces
that. Once `add-candidates` rounds exist, `init` refuses to run rather than clobbering them.
Use `--max-cases` for a quick smoke test before a full run.

## How it works

```
seeds (today's strategies) ─► evaluate live ─┐
                                             ├─► leaderboard ─► report.md
field probe (cached) ─► diagnostics ─► AGENT proposes candidates (query type, fields, boosts)
        │                                    │
        └─► local re-rank ◄── OPTIMIZER tunes boosts ──► confirm live ─┘
```

1. **Seeds** — the current hand-authored strategies (`benchmark/strategies.py`) become preset
   candidates and are scored live for a baseline, built from the table's `fields.yaml` boosts
   — or, if none exists, from a field list bootstrapped by profiling the index. If the
   fields.yaml also carries a `query:` block (a config someone already applied), that exact
   config joins the board as the **`deployed_config`** seed, and the report gains a second
   headline: *did this run beat what's actually running?* — the question that decides whether
   re-deploying is worth it, which "beat the stock frontend query" doesn't answer.
1b. **Analyzer reachability** (`index_profile.analyzer_reachability`) — two live queries per
   field ask whether a *lowercase* query can match it at all. Columns the index's
   SearchConfiguration routed to the KEYWORD analyzer match their whole value exactly and
   case-sensitively (`fundingAgency:NTAP` → 101 hits, `fundingAgency:ntap` → 0), which is
   invisible to the column type and looks merely uninformative in the probe. Verdicts land in
   `round_context.json` as `field_reachability` / `unreachable_fields` / `reachability_note`.
   They are **flagged, not dropped**: such a field is dead for lowercase queries but still
   matches a user who types the stored casing, so the note is read against the golden set's own
   queries — if every one is lowercase, it says so plainly and the agent should stop proposing
   those fields.
2. **Field probe** — each match field is run as its own single-field query per golden case,
   once, and cached to `<out>/<table>/probe.json`. This powers two things:
   - **Diagnostics** the agent reads — for each case (worst-first), which fields each ideal doc
     matches or misses, and the non-relevant *distractors* still outranking the best ideal doc
     with the field each wins on. These are computed through the **current leaderboard
     winner's** field weighting and ordered by its live per-case score, so the signal shifts
     round to round as the winner improves (not a fixed equal-weight baseline).
   - A **local re-ranker** — for `best_fields`/`most_fields` the combined score is just
     `max`/`sum` of boosted per-field scores, so the optimizer can score *any* boost vector by
     recombining cached scores, with **zero** new live queries.
3. **Agent proposals** — the calling agent reads `round_context.json` (profile + leaderboard +
   diagnostics) and writes new candidate *structures* (query type, field selection, starting
   boosts) with rationale to `candidates.json`, matching the `candidate_schema` the context
   carries; `add-candidates` validates and normalizes every one before it's tried.
4. **Numeric optimizer** — coordinate ascent + random restarts over each candidate's boosts.
   Free via the probe for decomposable candidates. Non-decomposable ones (cross_fields, phrase,
   fuzzy, etc.) are warm-started from a free probe sweep over the same fields, then refined with
   live queries under a hard eval budget (`optimize.LIVE_EVAL_BUDGET`) so cost stays bounded.
5. **Live confirm** — every optimized candidate is re-scored with a real `multi_match` against
   the index, so the leaderboard numbers are ground truth (see the caveat below). When a round
   doesn't improve the objective, `round_context.json` bumps `stale_rounds` and recommends
   finalizing rather than proposing again.
6. **Full-set winner verification** — when `--max-cases` restricts tuning to a slice, the
   leaderboard holds *slice* scores. At finalize the winner **and**
   `frontend_default` are re-scored live on the *full* golden set, and that becomes the
   authoritative headline; `report.md`/`leaderboard.json` flag the slice (`scored_on_slice`,
   `n_cases_used`/`n_cases_total`). With no slice, tuning already covered every case and this
   step is skipped. A recommendation must never reported on a subset of the golden set.

### Decomposable vs non-decomposable candidates

This is the single most important distinction for understanding what the harness does well and
where it's expensive — it's the reason boost tuning is sometimes free and sometimes slow.

A candidate is **decomposable** when its combined relevance score is a simple function of the
per-field scores the probe already measured: `best_fields` = `max(boost × field_score)` and
`most_fields` = `sum(boost × field_score)`, over an explicit field set, with **no** fuzziness,
`minimum_should_match`, or `phrase_boost`. For these, *any* boost vector can be re-scored by
recombining the cached probe matrix — so the numeric optimizer runs a full multi-restart sweep
at **zero live queries**. This is the harness's core leverage, and where it genuinely excels:
finding good per-field weights for `best_fields`/`most_fields` is fast, exhaustive, and cheap.

A candidate is **non-decomposable** when its score can't be reconstructed that way:
- **`cross_fields`** — treats several fields as one merged field for term-frequency purposes, so
  a doc's score depends on cross-field term stats the single-field probe never saw.
- **`phrase` / `phrase_prefix`** — order- and adjacency-sensitive; a phrase match isn't any
  function of independent per-field term scores.
- **`simple_query_string`** — different query parser/semantics entirely.
- **any `fuzziness`, `minimum_should_match`, or `phrase_boost`** — each adds matches (or gates
  them) in ways the probe's exact single-field queries didn't capture.

For these the probe can't predict the ranking, so their boosts are tuned with **live queries**.
To keep that both effective and bounded, the optimizer (`optimize.py`):
1. **Warm-starts from a free probe sweep** — first tunes the boosts against a decomposable
   surrogate (`best_fields` over the same fields, scored on the probe) to get a strong prior for
   "which fields deserve weight," which transfers well even though the exact ranking differs;
2. **Caps live evaluations** at `LIVE_EVAL_BUDGET` (default 40) distinct configs, keeping partial
   progress if it stops early — so cost is predictable regardless of field count.

**Why this matters in practice.** Non-decomposable candidates are ~5× the wall-clock of a
decomposable one (each is `≤ LIVE_EVAL_BUDGET × cases` live queries vs. zero). They're worth
proposing when the diagnostics call for them — a `cross_fields` config for names split across
columns, a phrase boost for order-sensitive queries — but stacking several in one batch is what
turns a tens-of-minutes run into an hour. The harness surfaces this: `add-candidates` prints how
many candidates in a batch need live optimization, and each prints a `[probe]` vs `[live (slower)]`
tag as it runs. It's also a mild structural bias to be aware of: because decomposable candidates
are tuned far more thoroughly (full sweep vs. a budgeted one), the leaderboard leans slightly in
their favor — a real signal (they *are* cheaper to tune well), but not proof the query type is
inherently better.

### The probe caveat

A single-field query's BM25 score isn't byte-identical to that field's internal contribution
inside a combined `multi_match` (length norms / term stats differ). So the probe drives the
*search* over boost space efficiently, but the **final number always comes from a live query**.
That's why winners are re-confirmed, and why you should re-run the benchmark after applying one.

## What it's good for, and what it isn't

**Excels at:**
- **Query-time boost/field tuning without query logs** — the golden set is the objective, so it
  works on any public SearchIndex with no UBI/click data and no backend access.
- **Finding per-field weights for `best_fields`/`most_fields`** — thorough and effectively free
  via the probe (see above).
- **Grounded, honest reporting** — every leaderboard number is a real live query, the winner is
  always compared against the live `frontend_default`, and (under `--max-cases`) re-verified on
  the full golden set so a recommendation is never quoted on a subset.
- **A real round-to-round feedback loop** — diagnostics re-compute against the current winner
  each round (which distractors still outrank the ideal docs, on which field), so an agent's
  later proposals target the cases the current best config still gets wrong.

**Caveats / limitations:**
- **Query-time only.** It never changes the index, analyzers, or `SearchConfiguration` — no
  synonym/stemming/tokenizer tuning (that's a separate leverage path). If a case
  fails because the *analyzer* drops the match entirely, no boost vector can fix it.
- **Only as good as the golden set** It optimizes exactly what the golden encodes; gaps or
  biases in the relevant sets propagate straight into the "winner."
- **Non-decomposable candidates are slower and less exhaustively tuned** — bounded and
  warm-started now, but still a budgeted live search, not the free full sweep (see above).
- **The probe is an approximation** for driving the search (the probe caveat above) — mitigated
  by always re-confirming live, never trusted as the final number.

## Where artifacts go

Every subcommand takes `--bench DIR` (READS `<DIR>/<table>/{golden,fields}.yaml`) and
`--out DIR` (WRITES `<DIR>/<table>/`). Both default to the directory you invoke from:
`./benchmark/` and `./tuner-runs/`. No directory in this repo is reserved for run artifacts —
`tuner-runs/` is gitignored working state, and the Managed Agent is handed a writable sandbox
path instead.

## Outputs (`<out>/<table>/`)

- `leaderboard.json` — every distinct candidate tried, ranked by the objective, with full
  configs, plus `fields_bootstrapped` (true if no `fields.yaml` existed for this table and the
  field list came from profiling instead) and, under `--max-cases`, the slice fields
  `scored_on_slice` / `n_cases_used` / `n_cases_total` and the winner's authoritative full-set
  score `winner_objective_full` (leaderboard entries themselves hold the slice scores).
- `report.md` — the experiment log: opens with a slice callout if `--max-cases` was used (and a
  bootstrapped-fields callout if the field list came from profiling), then **always states the
  winner's objective score vs. the default frontend query** (`frontend_default`, on the full
  golden set) — a clear improvement delta, or an explicit "no improvement found" if the default
  remains best — followed by the leaderboard table (slice scores, clearly labelled), the winning
  config, and the full-set per-case winner-vs-baseline deltas. (This is the experiment artifact;
  the stakeholder-facing `benchmark/<table>/RESULTS.md` is separate and unchanged.)
- `tuned_fields.yaml` — the winning config, formatted as a drop-in `fields.yaml`: the boosts
  under `fields:` and the query shape under `query:` (see "Applying a winner"), so applying it
  reproduces the scored config rather than approximating it. The header notes the winning
  query type and the full-set objective score it was verified at.
- `probe.json` — cached probe matrix (delete to force a re-probe).

**Saturated boosts.** When the optimizer leaves a boost at its 10.0 ceiling, `report.md` opens
with a callout and `leaderboard.json` carries `winner_saturation`. Read it carefully: ranking
depends only on the *ratios* between boosts (scaling them all leaves the order identical), so a
pinned boost is **not** a truncated search — the same ratio is reachable by lowering the other
fields. It means that field dominates by ~10:1, which on a few dozen cases is an overfitting
smell. So each pinned boost is re-scored at half its value (free, off the probe):

- **no change** → the value is arbitrary within a plateau; prefer the smaller, less extreme config.
- **score drops** → the ranking genuinely hinges on that one field. Real, but brittle — widen the
  golden set before trusting it.

Non-decomposable candidates are skipped **per round** rather than charged the extra live
queries — but at `finalize` the *winner* gets the check paid for with live queries if it never
had a free one (capped at `MAX_LIVE_SATURATION_FIELDS`, with any skipped field named). The
final report is the one a maintainer acts on; it shouldn't be quieter about brittleness than
the per-round output was.

**Near-ties.** `finalize` also reports any config within `NEAR_TIE_EPS` (0.005) of the winner
that uses **fewer knobs** — `near_ties` in `leaderboard.json`, a callout in `report.md`. On a
few dozen cases nDCG moves in steps of ~0.01, so a winner can edge out a plainer config by an
amount that is pure noise and still be reported as *the* recommendation (a `phrase_boost` worth
+0.0003 is how that shows up in practice). The board is not reordered — the measured number
stands — but the reviewer gets told they can take the simpler config for free.
- `state.json` / `round_context.json` — the agent-driven path's working state (the full golden,
  cached profile, board) and the per-round context the agent reads. Transient, not deliverables.

## Applying a winner

```bash
cp tuner-runs/tools/tuned_fields.yaml benchmark/tools/fields.yaml       # review first
python3 benchmark/run.py tools --strategy tuned --label tuned           # confirm live
```

`tuned_fields.yaml` is a complete `fields.yaml`, carrying **both halves** of the winner:

```yaml
fields:                     # which columns, and their boosts
  - "manifestation^10"
  - "studyName^6"
  - "summary^3.5"

query:                      # the query SHAPE the boosts were tuned for
  query_type: multi_match
  multi_match_type: best_fields
  tie_breaker: 0.5
  phrase_boost:
    fields: [studyName, summary]
    boost: 4.0
```

The `query:` block is optional and its keys are exactly `candidate.py`'s candidate schema.
`run.py` compiles it — through `candidate.build_dsl()`, the same function that scored the
winner — and registers it as the **`tuned`** strategy for that table. Tables without a block
(the untuned default) are unaffected and keep only the fixed strategies.

This exists because the fixed strategies in `benchmark/strategies.py` take no knobs, so a
winner using `tie_breaker`, `minimum_should_match`, or `phrase_boost` previously had nothing
that reproduced it: you could apply its boosts, but re-running scored a *different query* than
the one recommended. Boosts stay in `fields:` and are never duplicated inside the block, so
there is one field list per table rather than two that can disagree.

`web/strategies.js` mirrors the compiler for the browser Search Lab (`build_site.py` ships the
block in each table's data JSON), so the lab offers the same `tuned` recipe rather than
applying tuned boosts to a stock query shape.
