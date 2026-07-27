"""Field probe: per-(case × field) single-field scores.

Runs each match field as its *own* single-field query for every golden case once, capturing
the per-doc BM25 score in that field. This is the harness's leverage point:

  1. **Local re-rank** — for query types whose combined score decomposes into per-field
     contributions (`best_fields` = max, `most_fields` = sum), any boost vector can be scored
     by recombining the cached per-field scores, with **zero** new live queries. That makes
     the numeric boost sweep essentially free.
  2. **Diagnostics** — for each ideal (relevant) doc we can see which fields it matched and at
     what score, which fields it missed entirely, and — under the current leaderboard winner's
     field weighting — which non-relevant docs outrank it and on what field. The signal the
     proposal agent reads to reason about *why* a case still fails.

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
ROOT = os.path.dirname(os.path.dirname(HERE))     # sciops/tuning -> sciops -> repo root
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


def profile_brief(profile):
    """Compact a `profile_index.profile()` result for a prompt / round_context.json — the
    roles map and per-column category vocab are the useful parts, the raw histogram isn't."""
    return {
        "total_hits": profile.get("total_hits"),
        "roles": profile.get("roles"),
        "columns": [{"name": c["name"], "role": c["role"], "fill": c["fill"],
                     "samples": c["samples"]} for c in profile.get("columns", [])],
    }


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


def _combined_scores(fields_boosts, case_probe, combine_sum):
    """Per-doc combined score + the single field that contributes most to it, under a
    {field: boost} map. best_fields -> max(boost*field_score); most_fields -> sum. Returns
    {doc: {"score": float, "top_field": str}}."""
    scores = case_probe["scores"]
    agg = {}
    for field, boost in fields_boosts.items():
        for doc, s in scores.get(field, {}).items():
            contrib = boost * s
            cur = agg.get(doc)
            if cur is None:
                agg[doc] = {"score": contrib, "top_field": field, "_top": contrib}
            else:
                cur["score"] = cur["score"] + contrib if combine_sum else max(cur["score"], contrib)
                if contrib > cur["_top"]:
                    cur["_top"], cur["top_field"] = contrib, field
    return agg


def rerank(candidate, case_probe):
    """Locally re-rank a case from the probe matrix under the candidate's fields+boosts.
    best_fields -> per-doc score = max(boost*field_score); most_fields -> sum. Returns ranked
    ids (best first). Only valid when is_local_rerankable(candidate)."""
    combine_sum = candidate.get("multi_match_type") == "most_fields"
    agg = _combined_scores(candidate["fields"], case_probe, combine_sum)
    return [d for d, _ in sorted(agg.items(), key=lambda kv: kv[1]["score"], reverse=True)]


def rerank_ranker(candidate, probe_matrix):
    """A ranker(query, size) (the evaluate.py interface) backed by the probe — used by the
    optimizer to score boost vectors without live queries. Keyed by query text."""
    by_query = {c["query"]: c for c in probe_matrix["cases"].values()}

    def rank(query, size):
        cp = by_query.get(query)
        return rerank(candidate, cp)[:size] if cp else []
    return rank


def diagnose(probe_matrix, golden, winner=None, winner_per_case=None, objective="ndcg",
             max_cases=12):
    """Per-case relevance diagnostics for the proposal agent.

    For each (worst-first) case: which fields each ideal doc matched / missed, which ideal docs
    no probed field matched at all (a recall gap), and — the actionable part — the `distractors`
    outranking the best ideal doc under the CURRENT leaderboard winner's field weighting, each
    with the field it wins on (so the agent can see which field to down-weight, not just guess).

    The lens is the current best candidate, not a fixed equal-weight baseline, so diagnostics
    actually change round to round as the winner improves:
      - `winner` (the leaderboard-best candidate) supplies the field weighting for the distractor
        analysis; falls back to equal weight over all probed fields when absent (round 0 / a
        winner with no explicit fields, e.g. frontend_default).
      - `winner_per_case` (the winner's live {case_id: per_case_scores}) drives the ordering, so
        the agent sees the cases the current best config still gets wrong. Falls back to the
        winner's probe-estimated rank of the best ideal doc when live per-case data is absent.
    """
    from evaluate import OBJ_CASE_KEY  # local import: probe.py has no other evaluate dependency
    case_key = OBJ_CASE_KEY[objective]
    fields = probe_matrix["fields"]
    # Field weighting for the distractor lens: the winner's explicit boosts, else equal weight.
    wfields = (winner or {}).get("fields") or {f: 1.0 for f in fields}
    combine_sum = (winner or {}).get("multi_match_type") == "most_fields"

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

        # Distractors: under the winner's weighting, which non-relevant docs outrank the best
        # ideal doc, and on which field do they win.
        combined = _combined_scores(wfields, cp, combine_sum)
        relevant = set(case["relevant"])
        best_rel = max((combined[r]["score"] for r in relevant if r in combined), default=0.0)
        distractors = sorted(
            ({"id": d, "score": round(v["score"], 3), "won_on": v["top_field"]}
             for d, v in combined.items() if d not in relevant and v["score"] > best_rel),
            key=lambda x: x["score"], reverse=True)[:5]

        # ordering signal: the winner's live per-case objective if we have it, else the winner's
        # probe-estimated rank of the best ideal doc (lower rank = better).
        wpc = (winner_per_case or {}).get(case["id"])
        if wpc is not None:
            order_score = wpc.get(case_key, 0.0)   # higher = better → invert for worst-first
        else:
            ranked = [d for d, _ in sorted(combined.items(),
                                           key=lambda kv: kv[1]["score"], reverse=True)]
            ranks = [ranked.index(r) + 1 for r in relevant if r in ranked]
            order_score = 1.0 / min(ranks) if ranks else 0.0

        out.append({"id": case["id"], "query": case["query"], "type": case.get("type"),
                    f"winner_{objective}": round(order_score, 4),
                    "relevant": rel_diag, "unmatched_relevant": unmatched,
                    "distractors_outranking_best_ideal": distractors})
    # worst first (lowest winner objective / rank signal)
    out.sort(key=lambda d: d[f"winner_{objective}"])
    return out[:max_cases]
