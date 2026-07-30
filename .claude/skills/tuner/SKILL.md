---
name: tuner
description: Recommend a better query-time search config (query type, field boosts) for a Synapse SearchIndex — identified by index id, index name, or its source table id — by optimizing against its existing golden relevance set, including proposing entirely new query structures via an agentic loop. Only needs a golden relevance set — if the index has no existing search strategy (fields.yaml) to compare against, one is bootstrapped by profiling it; if no index exists at all, this skill doesn't apply and should not be used! Use when the user asks to tune search ranking, adjust field boosts, improve recall/nDCG, or investigate why certain queries rank poorly for an index/table ("tune ranking for better project search results", "why does this query rank the wrong things", "improve recall for datasets").
dependencies: python>=3.8, pyyaml>=5.1
---

# Automated search-relevance tuning

Given a **SearchIndex** — identified by index id, index name, or its source table
id (see "Resolving what to tune" below) — run the tuning harness to find a better
query-time config, evaluated against its existing `golden.yaml` relevance set — proposing
entirely new candidate structures yourself, not just re-weighting an existing one — then
summarize the result. An existing `fields.yaml` (a current hand-tuned strategy) is a
nice-to-have starter for comparison, but plenty of tables don't have one yet. If
none exists, the harness bootstraps a starting field list itself by profiling the live
index, so you shouldn't need to ask the user to supply one.

This wraps a standalone harness (`tune.py` and its siblings) that treats the golden
set as the objective function, so no query logs or click data are needed. It tunes
**query-time knobs only** — query type, which fields to search, per-field boosts, fuzziness,
`tie_breaker`, `minimum_should_match`, phrase boosting. It never touches the live index,
analyzers, or `SearchConfiguration`.

**You are the proposal step.** *You* read the diagnostics it produces and 
author new candidate structures yourself, using your own reasoning. 
A numeric optimizer then tunes each of your proposals' boosts, 
and every candidate is confirmed with a real live query, so the leaderboard is ground truth.

## Where to read and write

The harness never writes beside its own code, because wherever it's installed may be read-only. 
These parameters on the `tune.py` command say where data lives:

- `--bench DIR` — READS `<DIR>/<table>/{golden,fields}.yaml` by default
- `--out DIR` — WRITES `<DIR>/<table>/` artifacts (probe cache, `round_context.json`,
  `leaderboard.json`, `tuned_fields.yaml`, `report.md`)

Both default to the directory you run from (`./benchmark/` and `./tuner-runs/`), so if working locally
you can omit them entirely. Anywhere else, other instructions or context should tell you the two
directories to use — substitute `<BENCH_DIR>` and `<OUT_DIR>` and pass them on
**every** invocation rather than relying on an exported variable: each command may run in a
fresh shell, so an export can silently fail to carry over and the harness would fall back to
its own (possibly read-only) directory and fail on the first write.

**Resolving what to tune.** You'll be given ONE of these — try them in this priority order
until one resolves to a real, already-built SearchIndex:
1. **An index id** (`synXXXXXXXX`, a SearchIndex) — the preferred input. Confirm it's a real
   SearchIndex before proceeding:
   ```bash
   curl -s "https://repo-prod.prod.sagebase.org/repo/v1/entity/<INDEX_ID>" \
   | python3 -c "import sys,json; e=json.load(sys.stdin); print(e.get('concreteType'), e.get('name'))"
   ```
   If that 404s or the `concreteType` doesn't contain `SearchIndex`, treat it as not
   resolved (fall through to declining below — don't guess at a different id).
2. **An index name** (e.g. `nf-tools`) — check it against the master index collections
   project (`syn74909065`) and resolve it to an id:
   ```python
   import sys; sys.path.insert(0, ".claude/skills/tuner/scripts/tuning")
   from client import resolve_index_name
   index_id, index_name = resolve_index_name("nf-tools")   # (None, None) if no such index
   ```
3. **A source table id** (e.g. `syn51730943` — NOT a SearchIndex) — find the SearchIndex
   built from it, the reverse of goldie's own index→table lookup:
   ```python
   from client import resolve_table_to_index
   index_id, index_name = resolve_table_to_index("syn51730943")   # (None, None) if none built yet
   ```

**If none of these resolves to an existing SearchIndex, decline the job outright.** Say so
plainly and stop — do not guess at a different index, and do not try to get one built
(that's out of scope, tuning fundamentally needs an admin-created *live* index to query). 
This is a different, earlier stop condition than the "no golden.yaml" one below —
this one means there's nothing to tune at all, full stop.

**Next, getting the table's golden.yaml.**: you truly can't proceed without it 
(it's the objective function). Once you have a resolved `index_id`, check, in order:
1. **A source explicitly given to you.** If your instructions include a specific location/repo/URL, fetch
   from there.
2. **Already present**: `<BENCH_DIR>/<table>/golden.yaml` exists
   when you start — something upstream (a coordinating agent, or an uploaded
   file) already placed it there. Don't re-fetch or overwrite it. Here, the `benchmark/<table>/` folder (if any) 
   holds its golden set — **search, don't guess**: the folder name is *not* a deterministic 
   transform of the index name (e.g. the index `nf-publications-v2` could live in folder 
   `usage-publications`, not `publications-v2`). Check every already-mounted/fetched
  `benchmark/*/golden.yaml` for the one whose header `index:` matches your resolved 
  `index_id`; that folder's name is the `<table>` argument the rest of this doc (and `tune.py`) expects. 
3. **Otherwise: stop.** With no source given or nothing already present, you have no golden
   set to work from, and it's not your job to create one. Do not guess at or assume a repo, 
   stop and state the blocker. 

Note in your summary which source you used (pre-supplied / a given source) to be clear about provenance.

**Again, `fields.yaml` (an existing search strategy) is optional.** If it
*doesn't* exist — the common case for a table that's never been tuned — don't ask the user
for one: `tune.py init` bootstraps a starting field list
itself by profiling the live index (names/categories/free text; identifiers excluded) with its
own `index_profile.py`. It prints an expected
`no fields.yaml for <table> — bootstrapping ...` when it does this,

Your three deliverables are `leaderboard.json`, `tuned_fields.yaml`, and `report.md`; 
if the caller gave you an outputs directory, make sure they're there.

## Parameters

You'll be given an index id, index name, or source table id (covered in "Resolving what to tune"
above) and optional intent for:
- `--objective {ndcg|mrr|recall|hit1|hitk}` (default `ndcg`)
- how many rounds of proposals to try — default to **3 rounds of ~6 candidates**, use your
  judgment, and stop early once `round_context.json`'s `stale_rounds` says there's been no
  improvement for 2 rounds, or sooner if you're confident you've found a strong winner. This
  is a *target*, not a hard rule: the harness itself enforces a hard ceiling regardless — **8
  rounds max**, **10 candidates max per `add-candidates` call** (`round_context.json`'s
  `max_rounds`/`max_candidates_per_round`/`rounds_remaining` say exactly where you stand).
  Past the round cap, `add-candidates` refuses outright and tells you to `finalize` instead;
  submit more than the per-round cap and it silently truncates to the first N. Don't rely on
  hitting these — treat `rounds_remaining` as a countdown and finalize before it runs out.
- `--max-cases N` (fast smoke test on a slice — use this if the user says "quick"/"smoke test").
  When you use it, the rounds tune against the first N cases only, so **the leaderboard and
  per-round scores you read are slice scores, not full-golden scores** — don't report a
  round score as the table's overall result. `finalize` automatically re-scores the winner
  **and** `frontend_default` on the *full* golden set with live queries and makes that the
  headline; `report.md`/`leaderboard.json` mark the slice (`scored_on_slice`, `n_cases_used`/
  `n_cases_total`) and the full-set number is the one to quote. You never re-run anything by
  hand for this — finalize does it — but do say in your summary that tuning used an N/total
  slice, since that's what the round-to-round exploration actually saw.

## Implementation

This is a loop where **you** are one of the steps:

```bash
python3 tune.py init <table> \
  --bench <BENCH_DIR> --out <OUT_DIR> [--objective ndcg] [--max-cases N]
```

This loads (or bootstraps, per above) the field list, evaluates the seed strategies live for
a baseline, probes every match field per golden case once (cached), and numerically
optimizes the decomposable seeds' boosts — all before you're needed. It writes
`<OUT_DIR>/<table>/round_context.json` containing: 
- `profile`: what the index/table is about
- `golden_summary`
- `allowed_fields`
- `current_default_boosts`
- `fields_bootstrapped`: true if there was no existing `fields.yaml` and this field list came from profiling instead
- `leaderboard`: best candidates so far 
- `diagnostics`: per-case, worst-first under the current best config; which fields the ideal docs match or miss
- `distractors_outranking_best_ideal`: non-relevant docs still beating the best ideal doc
and the field each one wins on; this is *why* cases fail, and a distractor winning on a field
is a direct hint to down-weight it. The diagnostics re-compute against the current leaderboard
winner each round, so they shift as you improve (re-read them every round, don't rely on the
first round's)
- `candidate_schema`: the exact JSON shape below
- `recommendation`

**Read `round_context.json`, then propose new candidate *structures*** — different query
types, field selections, and starting boosts, not just tweaks to existing ones. Use the
profile + leaderboard + diagnostics to reason about *why* cases fail, then propose fixes.
Vary what you return — don't submit near-duplicates; the numeric optimizer handles fine
boost-tuning, so focus on structure:
- `query_type`: `"multi_match"` (recommended) or `"simple_query_string"`.
- `multi_match_type`: `best_fields` (max over fields — good when one field should win),
  `most_fields` (sum — rewards matching in several fields), `cross_fields` (treats fields as
  one big field — good for names split across columns), `phrase`/`phrase_prefix`
  (order-sensitive).
- `fields`: which columns to search and each one's starting boost (>1 = more important).
  Pick from `allowed_fields` only.
- `fuzziness`: `"AUTO"` tolerates typos — **`best_fields`/`most_fields` only**; the live index
  rejects it outright for `cross_fields`, and it's meaningless for phrase types.
- `tie_breaker` (0–1, `best_fields` only): how much non-winning fields still contribute.
- `minimum_should_match`: e.g. `"2<75%"` to require more term overlap on longer queries.
- `phrase_boost`: optionally bump exact-phrase matches in some fields without requiring them.

> **Cost note — some knobs are ~5× slower to tune.** Plain `best_fields`/`most_fields`
> candidates (no fuzziness / phrase_boost / minimum_should_match) have their boosts tuned for
> free off the cached probe. Everything else — `cross_fields`, `phrase`/`phrase_prefix`,
> `simple_query_string`, or any candidate using `fuzziness` / `minimum_should_match` /
> `phrase_boost` — can't use the probe, so its boosts are tuned with live queries (bounded, but
> several minutes each). `add-candidates` prints how many candidates in your batch need live
> optimization. Do propose these structures when the diagnostics call for them — just don't
> stack many in one batch, and lean on the free decomposable ones for broad exploration.

Write your batch as a JSON file matching `round_context.json`'s `candidate_schema`:

```json
{"candidates": [
  {"name": "cross_fields_names", "rationale": "why this should rank better",
   "query_type": "multi_match", "multi_match_type": "cross_fields",
   "fields": [{"field": "resourceName", "boost": 5}, {"field": "synonyms", "boost": 3}],
   "fuzziness": null, "tie_breaker": null, "minimum_should_match": null, "phrase_boost": null}
]}
```

Then feed it to the harness, which normalizes, numerically optimizes each proposal's boosts,
and confirms every candidate live:

```bash
python3 tune.py add-candidates <table> candidates.json \
  --bench <BENCH_DIR> --out <OUT_DIR>
```

This prints each candidate's score and rewrites `round_context.json` with the updated
leaderboard/diagnostics/`stale_rounds` for your next round. Repeat propose → add-candidates
for a few rounds — your judgment. Aim for ~3 rounds of ~6 candidates and stop once
`stale_rounds` hits 2; that's a reasonable target, not a hard rule (the hard caps are
MAX_ROUNDS=8 and 10 candidates per call, enforced by the harness).

When done, finalize:

```bash
python3 tune.py finalize <table> --out <OUT_DIR>
```

This writes `leaderboard.json`, `tuned_fields.yaml`, `report.md` under
`<OUT_DIR>/<table>/`.

`init` only exits early on a missing `golden.yaml`, or on the rare case where profiling the
index for a bootstrap turns up no usable name/category/text columns at all — relay either
message directly, don't try to work around it (e.g. by inventing a `fields.yaml` yourself).
A missing `fields.yaml` alone is never a reason to stop — `init` handles that itself. (The
"no SearchIndex exists at all" decline from "Resolving what to tune" above happens earlier,
before you ever get here — that one's on you to catch, not `init`.)

## Summarizing

Copy the three output files to the caller's outputs directory, then report — **always lead with the
comparison against the default frontend query**:

- Which index you resolved to and how (given directly, by name, or via its source table —
  see "Resolving what to tune"), and which `benchmark/<table>/` folder you found for it —
  don't leave the reader to guess which table `tuned_fields.yaml` actually applies to.
- **First line: the recommended config's objective score vs. `frontend_default`, stated
  explicitly** — e.g. "Recommended config improves ndcg by +0.12 over the default frontend
  query (0.71 → 0.83)." `report.md`'s opening line already states this (it's always present,
  even when there's no improvement — say so plainly in that case too: "No improvement found;
  the default frontend query remains the best config"). **This number is always the full
  golden set**, even if you tuned on a `--max-cases` slice — finalize re-scored it there. If
  you did use a slice, add one clause noting tuning explored an N/total slice (the headline
  itself is still the full-set number).
- What changed structurally (query type, which fields gained/lost boost, and whether the
  winner came from a candidate *you proposed* vs. an *optimized seed* — `report.md`'s
  `source` column says which).
- 2-3 notable per-case swings from `report.md`'s per-case table, if any stand out.
- Which source you used for the table's golden data, and whether `fields.yaml` came from 
  an existing strategy or was bootstrapped by profiling the index (`report.md` has an opening callout when
  bootstrapped; `round_context.json`'s `fields_bootstrapped` also tells you) — if bootstrapped,
  say so plainly so the reader knows the "winner" isn't being compared against a prior
  hand-tuned config, just the live default.
- **How to apply it** — frame this as a handoff, not something the asker can run
  themselves: applying the recommendation means copying the output into wherever production configs are deployed, 
  which needs the appropriate person to review and write. Typically, a maintainer should add or replace
  `benchmark/<table>/fields.yaml` (but this path may be different depending on the prod repo setup) 
  with the attached `tuned_fields.yaml`, then confirm with
  `python3 benchmark/run.py <table> --strategy tuned`. `tuned_fields.yaml` is a complete
  config — the boosts under `fields:` **and** the query shape (query type, `tie_breaker`,
  `minimum_should_match`, `phrase_boost`) under `query:` — so it reproduces exactly what was
  scored. Don't tell the reader to apply the boosts alone; without the `query:` block the
  benchmark scores a different query.
