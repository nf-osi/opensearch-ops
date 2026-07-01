"""Field probe: per-(case × field) single-field scores.

Runs each match field as its *own* single-field query for every golden case once, capturing
the per-doc BM25 score in that field. This is the harness's leverage point:

  1. **Local re-rank** — for query types whose combined score decomposes into per-field
     contributions (`best_fields` = max, `most_fields` = sum), any boost vector can be scored
     by recombining the cached per-field scores, with **zero** new live queries. That makes
     the numeric boost sweep essentially free.
  2. **Diagnostics** — for each ideal (relevant) doc we can see which fields it matched and at
     what score, and which fields it missed entirely — the signal the proposal agent reads.

Caveat (why winners are always re-confirmed live): a single-field query's BM25 score is not
byte-identical to that field's internal contribution inside a combined `multi_match` (length
norms and term stats differ slightly). The probe drives the *search* over boost space; the
final number always comes from a real `multi_match` against the index (see tune.py)."""
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmark"))
from query import search                          # noqa: E402
from run import hit_id                            # noqa: E402
from candidate import DECOMPOSABLE                 # noqa: E402

PROBE_SIZE = 100  # API caps returned hits at 100 regardless


def _key(index, fields, golden):
    h = hashlib.sha256()
    h.update(index.encode())
    h.update("|".join(sorted(fields)).encode())
    for case in golden["cases"]:
        h.update(f"{case['id']}={case['query']}".encode())
    return h.hexdigest()[:16]


def is_local_rerankable(candidate):
    """True if the candidate's ranking can be reproduced from the probe matrix (so the boost
    sweep can avoid live queries). Conservative: plain best_fields/most_fields over an explicit
    field set, no fuzziness / phrase boost / minimum_should_match (those don't decompose or
    add matches the single-field probe didn't see)."""
    return (candidate.get("query_type", "multi_match") == "multi_match"
            and candidate.get("multi_match_type") in DECOMPOSABLE
            and bool(candidate.get("fields"))
            and candidate.get("fuzziness") is None
            and not candidate.get("phrase_boost")
            and not candidate.get("minimum_should_match"))


def probe(index, golden, fields, id_field, poll_s=0.15, cache_path=None, workers=8):
    """Build (or load) the per-(case × field) score matrix.

    Returns {index, fields, k, cases: {case_id: {query, relevant, scores: {field: {id: score}}}}}.
    Caches to `cache_path` keyed by (index, fields, golden queries). The (case × field) queries
    are independent, so they run concurrently (`workers`) to hide per-query job latency."""
    key = _key(index, fields, golden)
    if cache_path and os.path.exists(cache_path):
        try:
            cached = json.load(open(cache_path))
            if cached.get("key") == key:
                return cached
        except Exception:
            pass

    def one(case, f):
        dsl = {"query": {"multi_match": {"query": case["query"], "fields": [f],
                                         "type": "best_fields"}}, "size": PROBE_SIZE}
        res = search(index, dsl, response_parts=["HITS"], poll_s=poll_s)
        return case["id"], f, {hit_id(h, id_field): h.get("score", 0.0) for h in res.get("hits", [])}

    cases = {c["id"]: {"query": c["query"], "relevant": list(c["relevant"]), "scores": {}}
             for c in golden["cases"]}
    jobs = [(c, f) for c in golden["cases"] for f in fields]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for cid, f, scores in ex.map(lambda a: one(*a), jobs):
            cases[cid]["scores"][f] = scores
    out = {"key": key, "index": index, "fields": list(fields), "cases": cases}
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        json.dump(out, open(cache_path, "w"), indent=2)
    return out


def rerank(candidate, case_probe):
    """Locally re-rank a case from the probe matrix under the candidate's fields+boosts.
    best_fields -> per-doc score = max(boost*field_score); most_fields -> sum. Returns ranked
    ids (best first). Only valid when is_local_rerankable(candidate)."""
    combine_sum = candidate.get("multi_match_type") == "most_fields"
    scores = case_probe["scores"]
    agg = {}
    for field, boost in candidate["fields"].items():
        for doc, s in scores.get(field, {}).items():
            contrib = boost * s
            if combine_sum:
                agg[doc] = agg.get(doc, 0.0) + contrib
            else:
                agg[doc] = max(agg.get(doc, 0.0), contrib)
    return [d for d, _ in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)]


def rerank_ranker(candidate, probe_matrix):
    """A ranker(query, size) (the evaluate.py interface) backed by the probe — used by the
    optimizer to score boost vectors without live queries. Keyed by query text."""
    by_query = {c["query"]: c for c in probe_matrix["cases"].values()}

    def rank(query, size):
        cp = by_query.get(query)
        return rerank(candidate, cp)[:size] if cp else []
    return rank


def diagnose(probe_matrix, golden, max_cases=12):
    """Per-case relevance diagnostics for the proposal agent: for each (worst-first) case,
    which fields each ideal doc matched / missed, and which ideal docs no probed field matched
    at all (a recall gap given this field set). Ordered by how badly the equal-weight baseline
    ranks the case so the agent sees the hardest cases first."""
    fields = probe_matrix["fields"]
    out = []
    for case in golden["cases"]:
        cp = probe_matrix["cases"].get(case["id"])
        if not cp:
            continue
        scores = cp["scores"]
        rel_diag, unmatched = [], []
        for rid in case["relevant"]:
            matched = {f: round(scores[f][rid], 3) for f in fields if rid in scores.get(f, {})}
            if matched:
                missing = [f for f in fields if f not in matched]
                rel_diag.append({"id": rid, "matched_fields": matched, "missing_fields": missing})
            else:
                unmatched.append(rid)
        # equal-weight baseline rank of the best relevant doc (lower = better; None = unranked)
        eq = rerank({"multi_match_type": "best_fields", "fields": {f: 1.0 for f in fields}}, cp)
        ranks = [eq.index(rid) + 1 for rid in case["relevant"] if rid in eq]
        best_rank = min(ranks) if ranks else None
        out.append({"id": case["id"], "query": case["query"], "type": case.get("type"),
                    "best_relevant_rank_equal_weight": best_rank,
                    "relevant": rel_diag, "unmatched_relevant": unmatched})
    # worst (highest/None best_rank) first
    out.sort(key=lambda d: (d["best_relevant_rank_equal_weight"] is not None,
                            d["best_relevant_rank_equal_weight"] or 1e9), reverse=True)
    return out[:max_cases]
