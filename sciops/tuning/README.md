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
  `sciops/agents/goldie/`). An explicit `fields.yaml` (today's hand-tuned strategy, if any) is
  optional — the per-field probe can't run over a bare `*`, so if no `fields.yaml` exists,
  `load_table()` bootstraps a starting `{field: boost}` map by profiling the live index
  (names/categories/free text; identifiers excluded) instead of requiring one be provided.
- For the agentic loop via `run` (below): Anthropic API credentials (`ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN`, or `ant auth login`). `run --no-agent`, and the `init`/
  `add-candidates`/`finalize` path, need neither (see "Two ways to drive the agentic step").

## Usage

```bash
# Full loop: Claude proposes query types + field sets, the optimizer tunes boosts, all scored
# against the golden set; writes a leaderboard, a report, and a drop-in fields.yaml.
python3 sciops/tuning/tune.py run tools --rounds 3 --candidates-per-round 6 --objective ndcg

# Offline: no API key — just numerically optimize the current fields.yaml boosts.
python3 sciops/tuning/tune.py run tools --no-agent

# Fast smoke test: first 6 cases only.
python3 sciops/tuning/tune.py run tools --no-agent --max-cases 6
```

Options: `--objective {ndcg|mrr|recall|hit1|hitk}` (default `ndcg`), `--k`, `--model`
(default `claude-opus-5`), `--rounds` (default 3, hard-capped at `MAX_ROUNDS`=8),
`--candidates-per-round` (default 6, hard-capped at `MAX_CANDIDATES_PER_ROUND`=10),
`--max-cases N` (smoke test on a slice), `--workers` (default 8), `--poll`. Values above
either cap are silently clamped with a printed note, not rejected.

### Two ways to drive the agentic step

The "agent proposes candidates" step (below) has two independent implementations sharing the
same evaluate/probe/optimize code:

- **`run`** — one-shot, for a human at a terminal with their own `ANTHROPIC_API_KEY`. Calls
  `propose.py`, which makes its own direct Anthropic API call.
- **`init` / `add-candidates` / `finalize`** — for `sciops/agents/tuner/` (the Managed Agent behind
  the Slack bot). No API key touches this harness at all: the *calling* agent — already an
  LLM, running under the platform's own credentials — reads `sciops/tuning/<table>/round_context.json`
  (profile, leaderboard, diagnostics, and the exact candidate JSON schema), authors
  `candidates.json` itself, and drives the rounds:

  ```bash
  python3 sciops/tuning/tune.py init tools --objective ndcg
  # agent reads sciops/tuning/tools/round_context.json, writes candidates.json matching
  # its "candidate_schema" key
  python3 sciops/tuning/tune.py add-candidates tools candidates.json
  # repeat add-candidates for more rounds (round_context.json refreshes each time, including
  # a stale_rounds counter + recommendation) — up to MAX_ROUNDS=8; add-candidates refuses a
  # round past that (finalize instead), and truncates any batch over
  # MAX_CANDIDATES_PER_ROUND=10 candidates rather than rejecting it outright. Then:
  python3 sciops/tuning/tune.py finalize tools
  ```

**Query volume, cost, and how long it takes.** Runtime is dominated almost entirely by live
query latency — every score is an async Synapse job (~1–3s each), run `--workers` at a time.
Cost therefore tracks the *number of live queries*, which is predictable: the field probe is
`cases × fields` queries (one-time, then cached to `sciops/tuning/<table>/probe.json`), plus a
live confirm (`cases` queries) per candidate, plus the final full-set verification (`2 × cases`).
The expensive variable is how many **non-decomposable** candidates a round contains (see the
next section) — each adds up to `LIVE_EVAL_BUDGET × cases` queries. Rough, observed ballparks
for `nf-tools` (48 cases, ~15 fields), ±50% given Synapse's variable job queue:

| run | expect |
| --- | --- |
| smoke test (`--max-cases 6`, decomposable candidates) | a few minutes |
| full 48-case run, decomposable candidates only, 2–3 rounds | ~20–40 min |
| each non-decomposable candidate added | ~+5 min |
| full-set finalize verification | ~3–8 min |

**Checkpointing / resumability.** `add-candidates` persists the board after *every* candidate
(atomic write to `state.json`), so if a round is killed or times out partway, the candidates
already completed are kept — re-running `add-candidates` continues from them rather than
repeating minutes of live queries. Use `--max-cases` for a quick smoke test before a full run.

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
   — or, if none exists, from a field list bootstrapped by profiling the index.
2. **Field probe** — each match field is run as its own single-field query per golden case,
   once, and cached to `sciops/tuning/<table>/probe.json`. This powers two things:
   - **Diagnostics** the agent reads — for each case (worst-first), which fields each ideal doc
     matches or misses, and the non-relevant *distractors* still outranking the best ideal doc
     with the field each wins on. These are computed through the **current leaderboard
     winner's** field weighting and ordered by its live per-case score, so the signal shifts
     round to round as the winner improves (not a fixed equal-weight baseline).
   - A **local re-ranker** — for `best_fields`/`most_fields` the combined score is just
     `max`/`sum` of boosted per-field scores, so the optimizer can score *any* boost vector by
     recombining cached scores, with **zero** new live queries.
3. **Agent proposals** (Claude) — from the profile + leaderboard + diagnostics, propose new
   candidate *structures* (query type, field selection, starting boosts) with rationale.
   Structured output guarantees valid candidates.
4. **Numeric optimizer** — coordinate ascent + random restarts over each candidate's boosts.
   Free via the probe for decomposable candidates. Non-decomposable ones (cross_fields, phrase,
   fuzzy, etc.) are warm-started from a free probe sweep over the same fields, then refined with
   live queries under a hard eval budget (`optimize.LIVE_EVAL_BUDGET`) so cost stays bounded.
5. **Live confirm** — every optimized candidate is re-scored with a real `multi_match` against
   the index, so the leaderboard numbers are ground truth (see the caveat below). Rounds
   early-stop when the objective stops improving.
6. **Full-set winner verification** — when `--max-cases` restricts tuning to a slice, the
   leaderboard holds *slice* scores. At finalize (and at the end of `run`) the winner **and**
   `frontend_default` are re-scored live on the *full* golden set, and that becomes the
   authoritative headline; `report.md`/`leaderboard.json` flag the slice (`scored_on_slice`,
   `n_cases_used`/`n_cases_total`). With no slice, tuning already covered every case and this
   step is skipped. A recommendation is never reported on a subset of the golden set.

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
  synonym/stemming/tokenizer tuning (that's the separate privileged rebuild loop). If a case
  fails because the *analyzer* drops the match entirely, no boost vector can fix it.
- **Only as good as the golden set.** It optimizes exactly what the golden encodes; gaps or
  biases in the relevant sets propagate straight into the "winner." Build goldens from the source
  table, not the search index (see `goldie`).
- **Non-decomposable candidates are slower and less exhaustively tuned** — bounded and
  warm-started now, but still a budgeted live search, not the free full sweep (see above).
- **The probe is an approximation** for driving the search (the probe caveat above) — mitigated
  by always re-confirming live, never trusted as the final number.
- **Objective ≠ production.** nDCG/MRR on the golden set is a proxy; always re-run
  `benchmark/run.py` after applying a winner, and treat `tuned_fields.yaml` as a reviewed draft.

## Outputs (`sciops/tuning/<table>/`)

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
- `tuned_fields.yaml` — the winner's boosts, formatted as a drop-in `fields.yaml`; the header
  notes the winning query type and the full-set objective score it was verified at.
- `probe.json` — cached probe matrix (delete to force a re-probe).
- `state.json` / `round_context.json` — the agent-driven path's working state (the full golden,
  cached profile, board) and the per-round context the agent reads. Transient, not deliverables.

## Applying a winner

```bash
cp sciops/tuning/tools/tuned_fields.yaml benchmark/tools/fields.yaml   # review first
python3 benchmark/run.py tools --label tuned                    # confirm on the live index
```

If the winning **query type** differs from the current strategy (noted in `report.md` /
`tuned_fields.yaml` header), also add/adjust the matching strategy in `benchmark/strategies.py`
before re-running.
