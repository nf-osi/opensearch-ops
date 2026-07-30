"""Scoring core for the tuning harness.

Computes the same per-case metrics as `benchmark/run.py` (MRR / Recall@k / Hit@1 / Hit@k), so
tuning numbers are directly comparable to a benchmark run, and adds **nDCG@k** — a graded
metric that rewards getting the golden's *ranked head order* right, which is the better
objective for ranking optimization. Everything works off a `ranker(query, size) -> [id, ...]`
callable, so the same code scores both live index queries and the probe's local re-rank.

`reciprocal_rank`/`score_case` below are deliberately a second copy of run.py's definitions
rather than an import: keeping this skill self-contained means it can't reach into the
benchmark tree, which doesn't exist inside a published bundle. They are fixed textbook
definitions, but if you ever change one, change both — the comparability above is the whole
point of them matching.
"""
import math
from concurrent.futures import ThreadPoolExecutor

from candidate import build_dsl
from client import hit_id, search

OBJECTIVES = ("ndcg", "mrr", "recall", "hit1", "hitk")
# objective -> the key it reads in an `agg` dict (averaged over cases)
OBJ_AGG_KEY = {"ndcg": "ndcg_at_k", "mrr": "mrr", "recall": "recall_at_k",
               "hit1": "hit_at_1", "hitk": "hit_at_k"}
# objective -> the key it reads in a `per_case` entry (same, except mrr is "rr" per case)
OBJ_CASE_KEY = {**OBJ_AGG_KEY, "mrr": "rr"}


def reciprocal_rank(ranked_ids, relevant):
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in relevant:
            return 1.0 / i, i
    return 0.0, None


def score_case(ranked_ids, relevant, k):
    """MRR / Recall@k / Hit@1 / Hit@k for one case — same definitions as benchmark/run.py."""
    rel = set(relevant)
    topk = ranked_ids[:k]
    rr, rank = reciprocal_rank(ranked_ids, rel)
    found = rel & set(topk)
    return {
        "rr": rr,
        "first_rel_rank": rank,
        "recall_at_k": (len(found) / len(rel)) if rel else 0.0,
        "hit_at_1": 1.0 if (ranked_ids and ranked_ids[0] in rel) else 0.0,
        "hit_at_k": 1.0 if found else 0.0,
        "n_relevant": len(rel),
        "n_found_in_k": len(found),
    }


def ndcg_at_k(ranked_ids, relevant, k):
    """nDCG@k using the golden's ranked head as graded relevance: the first listed relevant
    id has the highest gain, decreasing down the list. Items outside the head get gain 0."""
    if not relevant:
        return 0.0
    gains = {rid: len(relevant) - i for i, rid in enumerate(relevant)}
    dcg = sum(gains.get(rid, 0) / math.log2(rank + 1)
              for rank, rid in enumerate(ranked_ids[:k], start=1))
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(rank + 1) for rank, g in enumerate(ideal, start=1))
    return (dcg / idcg) if idcg else 0.0


def score_ranked(ranked_ids, relevant, k):
    """score_case plus nDCG@k."""
    sc = score_case(ranked_ids, relevant, k)
    sc["ndcg_at_k"] = ndcg_at_k(ranked_ids, relevant, k)
    return sc


def evaluate(ranker, golden, k, workers=1):
    """Score `ranker` over every golden case. Returns {agg, per_case} where agg has
    mrr/recall_at_k/hit_at_1/hit_at_k/ndcg_at_k averaged over cases. `workers` > 1 runs the
    cases concurrently (each `ranker` call is an independent async index query, so overlapping
    them hides the ~seconds-long per-query job latency)."""
    cases = golden["cases"]

    def score(case):
        ranked = ranker(case["query"], max(k, 10))
        return case["id"], score_ranked(ranked, case["relevant"], k)

    if workers > 1 and len(cases) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            per_case = dict(ex.map(score, cases))
    else:
        per_case = dict(score(c) for c in cases)
    n = len(per_case) or 1
    keys = ("rr", "recall_at_k", "hit_at_1", "hit_at_k", "ndcg_at_k")
    agg = {("mrr" if key == "rr" else key): sum(c[key] for c in per_case.values()) / n
           for key in keys}
    agg["n_cases"] = len(per_case)
    return {"agg": agg, "per_case": per_case}


def objective_value(agg, objective):
    return agg[OBJ_AGG_KEY[objective]]


def live_ranker(index, candidate, id_field, poll_s=0.15):
    """A ranker that compiles the candidate to DSL, runs it against the live index, and
    returns the ranked id_field values (the real, ground-truth ranking)."""
    def rank(query, size):
        dsl = build_dsl(candidate, query, size)
        res = search(index, dsl, response_parts=["HITS", "TOTAL_HITS"], poll_s=poll_s)
        return [hit_id(h, id_field) for h in res.get("hits", [])]
    return rank
