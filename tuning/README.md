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

- A benchmark table at `benchmark/<table>/` with a `golden.yaml` (see the `generate-goldens`
  skill) **and** an explicit `fields.yaml` (the per-field probe can't run over a bare `*`).
- For the agentic loop: Anthropic API credentials (`ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN`, or `ant auth login`). The `--no-agent` path needs neither.

## Usage

```bash
# Full loop: Claude proposes query types + field sets, the optimizer tunes boosts, all scored
# against the golden set; writes a leaderboard, a report, and a drop-in fields.yaml.
python3 tuning/tune.py tools --rounds 3 --candidates-per-round 6 --objective ndcg

# Offline: no API key — just numerically optimize the current fields.yaml boosts.
python3 tuning/tune.py tools --no-agent

# Fast smoke test: first 6 cases only.
python3 tuning/tune.py tools --no-agent --max-cases 6
```

Options: `--objective {ndcg|mrr|recall|hit1|hitk}` (default `ndcg`), `--k`, `--model`
(default `claude-opus-4-8`), `--rounds`, `--candidates-per-round`, `--max-cases N` (smoke
test on a slice), `--workers` (default 8), `--poll`.

**Query volume.** Each Synapse search is an async job (~seconds of latency), and the harness
issues a lot of them — the field probe alone is `cases × fields` queries, plus a live confirm
per candidate. Queries are independent, so they run concurrently (`--workers`); the probe is
cached to `tuning/<table>/probe.json` so only the first run pays for it. Use `--max-cases` for
a quick smoke test before a full run.

## How it works

```
seeds (today's strategies) ─► evaluate live ─┐
                                             ├─► leaderboard ─► report.md
field probe (cached) ─► diagnostics ─► AGENT proposes candidates (query type, fields, boosts)
        │                                    │
        └─► local re-rank ◄── OPTIMIZER tunes boosts ──► confirm live ─┘
```

1. **Seeds** — the current hand-authored strategies (`benchmark/strategies.py`) become preset
   candidates and are scored live for a baseline.
2. **Field probe** — each match field is run as its own single-field query per golden case,
   once, and cached to `tuning/<table>/probe.json`. This powers two things:
   - **Diagnostics** the agent reads — which fields each ideal doc matches or misses, and what
     outranks it under equal weighting.
   - A **local re-ranker** — for `best_fields`/`most_fields` the combined score is just
     `max`/`sum` of boosted per-field scores, so the optimizer can score *any* boost vector by
     recombining cached scores, with **zero** new live queries.
3. **Agent proposals** (Claude) — from the profile + leaderboard + diagnostics, propose new
   candidate *structures* (query type, field selection, starting boosts) with rationale.
   Structured output guarantees valid candidates.
4. **Numeric optimizer** — coordinate ascent + random restarts over each candidate's boosts.
   Free via the probe for decomposable candidates; a lighter live-scored pass otherwise.
5. **Live confirm** — every optimized candidate is re-scored with a real `multi_match` against
   the index, so the leaderboard numbers are ground truth (see the caveat below). Rounds
   early-stop when the objective stops improving.

### The probe caveat

A single-field query's BM25 score isn't byte-identical to that field's internal contribution
inside a combined `multi_match` (length norms / term stats differ). So the probe drives the
*search* over boost space efficiently, but the **final number always comes from a live query**.
That's why winners are re-confirmed, and why you should re-run the benchmark after applying one.

## Outputs (`tuning/<table>/`)

- `leaderboard.json` — every distinct candidate tried, ranked by the objective, with full configs.
- `report.md` — the experiment log: leaderboard table, the winning config, and per-case
  winner-vs-baseline deltas. (This is the experiment artifact; the stakeholder-facing
  `benchmark/<table>/RESULTS.md` is separate and unchanged.)
- `tuned_fields.yaml` — the winner's boosts, formatted as a drop-in `fields.yaml`.
- `probe.json` — cached probe matrix (delete to force a re-probe).

## Applying a winner

```bash
cp tuning/tools/tuned_fields.yaml benchmark/tools/fields.yaml   # review first
python3 benchmark/run.py tools --label tuned                    # confirm on the live index
```

If the winning **query type** differs from the current strategy (noted in `report.md` /
`tuned_fields.yaml` header), also add/adjust the matching strategy in `benchmark/strategies.py`
before re-running.
