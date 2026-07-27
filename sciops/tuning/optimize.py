"""Numeric boost optimizer — coordinate ascent over per-field weights.

The agent picks *structure* (query type, which fields); this sweeps the *continuous* boost
weights, which an LLM shouldn't burn tokens guessing at. For decomposable candidates
(best_fields/most_fields, explicit fields) the objective is scored from the cached probe
matrix, so a full multi-restart sweep costs zero live queries.

Non-decomposable candidates (cross_fields / phrase / phrase_prefix / simple_query_string, or
anything with fuzziness / minimum_should_match / phrase_boost) can't be scored from the probe,
so their boosts are tuned with the caller's live scoring function — expensive (1 eval =
n_cases live queries). Two things keep that both cheap and effective: a **probe warm-start**
(boosts are first tuned for free against a decomposable surrogate of the same fields, giving
the live pass a strong prior instead of the raw proposed numbers), and a **hard live-eval
budget** (`LIVE_EVAL_BUDGET`) so cost is bounded and predictable no matter how many fields the
candidate has.

Coordinate ascent: hold all weights fixed, try a small set of multipliers on one field, keep
any strict improvement, repeat across fields for a few rounds. Extra/random restarts escape
local optima. Deterministic given a seed (no Math.random); accepts only strict improvements."""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from candidate import normalize                   # noqa: E402
from evaluate import evaluate, objective_value     # noqa: E402
from probe import is_local_rerankable, rerank_ranker  # noqa: E402

EPS = 1e-9
MULTIPLIERS = (0.0, 0.5, 1.5, 2.0, 4.0)  # relative to a field's current boost (1.0 = no change, omitted)
# Multiplier set for the live pass: same shape, one fewer step to spend the eval budget on
# more fields/rounds rather than finer per-field granularity.
LIVE_MULTIPLIERS = (0.0, 0.5, 2.0, 4.0)
# Hard ceiling on *live* score_fn evaluations per optimize_boosts call (each eval = n_cases
# live queries). Bounds the wall-clock/query cost of a non-decomposable candidate regardless
# of how many fields it has — coordinate ascent stops early once this many distinct configs
# have been scored. The probe path is free and ignores this.
LIVE_EVAL_BUDGET = 40


def _sig(fields):
    return tuple(sorted((k, round(v, 4)) for k, v in fields.items()))


def coordinate_ascent(candidate, score_fn, allowed_fields, multipliers=MULTIPLIERS,
                      rounds=3, restarts=2, max_boost=10.0, seed=0, max_evals=None,
                      extra_starts=()):
    """Maximize score_fn(candidate) over candidate['fields'] boosts. score_fn must be
    deterministic. Returns (best_candidate, best_score).

    `max_evals` caps the number of *distinct* score_fn calls (cache hits are free); when
    reached the sweep returns the best config found so far. `extra_starts` supplies additional
    seed configs to ascend from (e.g. a probe warm-start), tried before the random restarts."""
    fields = list(candidate.get("fields") or {})
    if not fields:                       # nothing to tune (e.g. frontend_default / all-fields)
        return candidate, score_fn(candidate)

    rng = random.Random(seed)
    cache = {}

    def over_budget():
        return max_evals is not None and len(cache) >= max_evals

    def scored(cand):
        s = _sig(cand["fields"])
        if s not in cache:
            cache[s] = score_fn(cand)   # callers gate new evals on over_budget() first
        return cache[s]

    def ascend(start):
        """Coordinate ascent from `start`. Keeps every improvement it finds before the eval
        budget runs out, then stops — partial progress is retained, not discarded."""
        cur = dict(start); cur["fields"] = dict(start["fields"])
        cur_score = scored(cur)
        for _ in range(rounds):
            improved = False
            for f in fields:
                base = cur["fields"].get(f, 1.0)
                for m in multipliers:
                    nb = round(min(base * m, max_boost), 4)
                    if abs(nb - base) < EPS:
                        continue
                    trial = dict(cur); trial["fields"] = dict(cur["fields"])
                    if nb <= 0:
                        trial["fields"].pop(f, None)        # boost 0 → drop the field
                        if not trial["fields"]:
                            continue
                    else:
                        trial["fields"][f] = nb
                    if _sig(trial["fields"]) not in cache and over_budget():
                        return cur, cur_score, True         # budget spent; keep progress so far
                    sc = scored(trial)
                    if sc > cur_score + EPS:
                        cur, cur_score = trial, sc
                        improved = True
            if not improved:
                break
        return cur, cur_score, False

    # Ascend from the candidate as given, then from each warm-start, then from random restarts,
    # keeping the best config across all of them — stopping once the eval budget is exhausted.
    best, best_score, exhausted = ascend(candidate)
    starts = list(extra_starts)
    for _ in range(restarts):
        init = dict(candidate)
        init["fields"] = {f: round(rng.uniform(0.5, 5.0), 2) for f in fields}
        starts.append(init)
    for start in starts:
        if exhausted:
            break
        cand, score, exhausted = ascend(start)
        if score > best_score + EPS:
            best, best_score = cand, score
    return best, best_score


def _probe_warm_start(candidate, golden, probe_matrix, objective, k, allowed_fields):
    """Tune this candidate's boosts for FREE against a decomposable surrogate (best_fields over
    the same fields, scored on the cached probe), returning just the tuned {field: boost} map.
    Used as a strong prior for the expensive live pass — the surrogate's ranking isn't identical
    to the real query type's, but "which fields deserve weight" transfers well. Returns None if
    the candidate has no explicit fields to tune."""
    if not candidate.get("fields"):
        return None
    surrogate = {"query_type": "multi_match", "multi_match_type": "best_fields",
                 "fields": dict(candidate["fields"])}

    def sf(c):
        ev = evaluate(rerank_ranker(c, probe_matrix), golden, k)
        return objective_value(ev["agg"], objective)

    tuned, _ = coordinate_ascent(surrogate, sf, allowed_fields)
    return tuned["fields"]


def optimize_boosts(candidate, golden, probe_matrix, objective, k, allowed_fields,
                    live_score_fn=None):
    """Tune a candidate's boosts. Decomposable candidates are swept against the probe matrix
    (free, full sweep); others use `live_score_fn` (a candidate->score callable), warm-started
    from a free probe sweep and capped at LIVE_EVAL_BUDGET live evaluations. Returns
    (best_candidate, best_score_on_chosen_metric).

    The returned score is the probe-estimate for decomposable candidates and the live score
    otherwise; either way tune.py re-confirms the winner with a live evaluate()."""
    candidate = normalize(candidate, allowed_fields)
    if is_local_rerankable(candidate):
        def sf(c):
            ev = evaluate(rerank_ranker(c, probe_matrix), golden, k)
            return objective_value(ev["agg"], objective)
        return coordinate_ascent(candidate, sf, allowed_fields)
    if live_score_fn is None:
        return candidate, None
    # Live scoring is expensive (1 eval = n_cases queries). Warm-start from a free probe sweep
    # over the same fields, then refine live under a hard eval budget — a fuller, better-primed
    # search than the old blind rounds=1/restarts=0 pass, at bounded and predictable query cost.
    warm = _probe_warm_start(candidate, golden, probe_matrix, objective, k, allowed_fields)
    extra_starts = ()
    if warm:
        primed = dict(candidate); primed["fields"] = warm
        extra_starts = (primed,)
    return coordinate_ascent(candidate, live_score_fn, allowed_fields,
                             multipliers=LIVE_MULTIPLIERS, rounds=2, restarts=0,
                             max_evals=LIVE_EVAL_BUDGET, extra_starts=extra_starts)
