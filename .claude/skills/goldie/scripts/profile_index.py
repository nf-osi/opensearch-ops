#!/usr/bin/env python3
"""Profile a SearchIndex to support golden-query generation.

Schema-agnostic: makes NO assumptions about column names (works across portals).
It samples documents and infers each column's *role* from its values — likely
identifier, name/title, low-cardinality category (good for topical queries), or
free text — plus fill rate and sample values.

Profiles the SearchIndex itself (via the SearchIndex query API) — for the ground-truth
*source table* behind it, see `profile_table.py` instead.

Anonymous; no token needed.

Invoking from the repo root:
  python3 .claude/skills/goldie/scripts/profile_index.py <INDEX_ID> [--n 100]
"""
import argparse, collections, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # synapse_client is a sibling — this skill is self-contained
from synapse_client import search, hit_dict  # noqa: E402


def values_of(v):
    """Normalize a field value into a list of scalar strings (decodes JSON arrays)."""
    if v is None:
        return []
    s = v if isinstance(v, str) else str(v)
    s = s.strip()
    if s in ("", "[]"):
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            pass
    return [s]


def trunc(s, n=60):
    s = str(s).replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


def _role(s):
    """Infer a column's role from its sample stats (heuristic — verify before relying)."""
    nd = len(s["distinct"])
    if s["fill"] >= 0.9 and s["n_vals"] and nd >= 0.95 * s["n_vals"] and s["avg_len"] <= 64:
        return "id?"          # high fill, ~all distinct → identifier candidate
    if 1 < nd <= 12 and s["avg_len"] <= 40:
        return "category"     # low cardinality, short values → topical/facet candidate
    if s["avg_len"] > 80:
        return "text"         # long values → free text / description
    if s["fill"] >= 0.5 and s["avg_len"] <= 64 and nd > 12:
        return "name?"        # high-variety short strings → name/title candidate
    return ""


def profile(index, n=100, poll_s=0.2):
    """Profile a SearchIndex from a <=n-row match_all sample. Returns a structured dict
    (no printing) for programmatic use (e.g. the tuning harness's proposal prompt):

      {index, total_hits, sampled,
       columns: [{name, role, fill, n_distinct, samples:[...], avg_len}],
       roles: {identifier:[...], name:[...], text:[...],
               category: {col: {value: count, ...}}}}

    Schema-agnostic: makes no assumptions about column names. The index sample is order-biased,
    so treat counts/coverage as a feel for the data, not ground truth (see SKILL.md)."""
    res = search(index, {"query": {"match_all": {}}, "size": n},
                 response_parts=["HITS", "TOTAL_HITS"], poll_s=poll_s)
    docs = [hit_dict(h) for h in res.get("hits", [])]
    if not docs:
        return {"index": index, "total_hits": res.get("totalHits"), "sampled": 0,
                "columns": [], "roles": {"identifier": [], "name": [], "text": [],
                                         "category": {}}}
    m = len(docs)
    cols = collections.OrderedDict((k, None) for d in docs for k in d)
    stats = {}
    for k in cols:
        vals, filled = [], 0
        for d in docs:
            vs = values_of(d.get(k))
            if vs:
                filled += 1
                vals.extend(vs)
        distinct = list(dict.fromkeys(vals))
        stats[k] = dict(fill=filled / m, n_vals=len(vals), distinct=distinct,
                        avg_len=(sum(len(x) for x in vals) / len(vals)) if vals else 0)

    columns = [{"name": k, "role": _role(stats[k]), "fill": round(stats[k]["fill"], 3),
                "n_distinct": len(stats[k]["distinct"]), "avg_len": round(stats[k]["avg_len"], 1),
                "samples": [trunc(x) for x in stats[k]["distinct"][:5]]}
               for k in cols]
    cat_dists = {}
    for k in cols:
        if _role(stats[k]) == "category":
            dist = collections.Counter()
            for d in docs:
                for v in values_of(d.get(k)):
                    dist[v] += 1
            cat_dists[k] = dict(dist.most_common(12))
    return {
        "index": index, "total_hits": res.get("totalHits"), "sampled": m,
        "columns": columns,
        "roles": {
            "identifier": [c["name"] for c in columns if c["role"] == "id?"],
            "name": [c["name"] for c in columns if c["role"] == "name?"],
            "text": [c["name"] for c in columns if c["role"] == "text"],
            "category": cat_dists,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index", help="SearchIndex id (synNNN)")
    ap.add_argument("--n", type=int, default=100, help="sample size (API caps returned hits at 100)")
    args = ap.parse_args()

    p = profile(args.index, args.n)
    if not p["sampled"]:
        print("no documents returned"); return
    print(f"index {p['index']}   totalHits={p['total_hits']}   sampled={p['sampled']}")
    print("(every hit also has a stable `rowId` from the index itself — usable as id_field "
          "if no column is a good identifier)\n")

    print("columns  (role | fill% | #distinct-in-sample | sample values):")
    for c in p["columns"]:
        print(f"  {c['role']:9} {c['name']:28} {int(c['fill']*100):3}%  "
              f"d={c['n_distinct']:<4} {c['samples']}")

    r = p["roles"]
    print("\nINFERRED ROLES (verify before relying on them):")
    print(f"  identifier candidates : {r['identifier'] or '(none — fall back to rowId)'}")
    print(f"  name/title candidates : {r['name'] or '(none obvious)'}")
    print(f"  free-text candidates  : {r['text'] or '(none)'}")
    print(f"  category columns (low-cardinality → good topical-query seeds):")
    for k, dist in r["category"].items():
        print(f"      {k}: {dist}")
    if not r["category"]:
        print("      (none)")


if __name__ == "__main__":
    main()
