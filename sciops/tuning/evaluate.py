"""Scoring core for the tuning harness.

Reuses benchmark/run.py's per-case scoring (MRR / Recall@k / Hit@1 / Hit@k) so the numbers
match the established benchmark exactly, and adds **nDCG@k** — a graded metric that rewards
getting the golden's *ranked head order* right, which is the better objective for ranking
optimization. Everything works off a `ranker(query, size) -> [id, ...]` callable, so the same
code scores both live index queries and the probe's local re-rank."""
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))     # sciops/tuning -> sciops -> repo root
sys.path.insert(0, HERE)                          # sibling tuning modules
sys.path.insert(0, ROOT)                          # query.py
sys.path.insert(0, os.path.join(ROOT, "benchmark"))  # run.py
from query import search, hit_dict                # noqa: E402
from run import score_case, hit_id                # noqa: E402  (reuse, don't duplicate)
from candidate import build_dsl                   # noqa: E402

OBJECTIVES = ("ndcg", "mrr", "recall", "hit1", "hitk")
# objective -> the key it reads in an `agg` dict (averaged over cases)
OBJ_AGG_KEY = {"ndcg": "ndcg_at_k", "mrr": "mrr", "recall": "recall_at_k",
               "hit1": "hit_at_1", "hitk": "hit_at_k"}
# objective -> the key it reads in a `per_case` entry (same, except mrr is "rr" per case)
OBJ_CASE_KEY = {**OBJ_AGG_KEY, "mrr": "rr"}


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
    """run.py's score_case plus nDCG@k."""
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
