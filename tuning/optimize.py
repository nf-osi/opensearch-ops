"""Numeric boost optimizer — coordinate ascent over per-field weights.

The agent picks *structure* (query type, which fields); this sweeps the *continuous* boost
weights, which an LLM shouldn't burn tokens guessing at. For decomposable candidates
(best_fields/most_fields, explicit fields) the objective is scored from the cached probe
matrix, so a full multi-restart sweep costs zero live queries. For other query types the
caller supplies a live scoring function and we run a lighter pass.

Coordinate ascent: hold all weights fixed, try a small set of multipliers on one field, keep
any strict improvement, repeat across fields for a few rounds. Random restarts escape local
optima. Deterministic given a seed (no Math.random); accepts only strict improvements."""
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


def _sig(fields):
    return tuple(sorted((k, round(v, 4)) for k, v in fields.items()))


def coordinate_ascent(candidate, score_fn, allowed_fields, multipliers=MULTIPLIERS,
                      rounds=3, restarts=2, max_boost=10.0, seed=0):
    """Maximize score_fn(candidate) over candidate['fields'] boosts. score_fn must be
    deterministic. Returns (best_candidate, best_score)."""
    fields = list(candidate.get("fields") or {})
    if not fields:                       # nothing to tune (e.g. frontend_default / all-fields)
        return candidate, score_fn(candidate)

    rng = random.Random(seed)
    cache = {}

    def scored(cand):
        s = _sig(cand["fields"])
        if s not in cache:
            cache[s] = score_fn(cand)
        return cache[s]

    def ascend(start):
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
                    sc = scored(trial)
                    if sc > cur_score + EPS:
                        cur, cur_score = trial, sc
                        improved = True
            if not improved:
                break
        return cur, cur_score

    best, best_score = ascend(candidate)
    for r in range(restarts):
        init = dict(candidate); init["fields"] = {
            f: round(rng.uniform(0.5, 5.0), 2) for f in fields}
        cand, score = ascend(init)
        if score > best_score + EPS:
            best, best_score = cand, score
    return best, best_score


def optimize_boosts(candidate, golden, probe_matrix, objective, k, allowed_fields,
                    live_score_fn=None):
    """Tune a candidate's boosts. Decomposable candidates are swept against the probe matrix
    (free, full sweep); others fall back to `live_score_fn` (a candidate->score callable)
    with a lighter pass. Returns (best_candidate, best_score_on_chosen_metric).

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
    # live scoring is expensive (1 eval = n_cases queries) — keep the sweep small
    return coordinate_ascent(candidate, live_score_fn, allowed_fields,
                             multipliers=(0.5, 2.0), rounds=1, restarts=0)
