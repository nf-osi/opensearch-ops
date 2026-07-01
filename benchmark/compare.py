#!/usr/bin/env python3
"""Compare two benchmark result files (e.g. baseline vs a new config run).

Prints a per-strategy table of the key metrics side by side with deltas, then
lists per-case movements (cases whose first-relevant rank improved or regressed)
so you can see *which* queries a config change helped or hurt.

Usage:
  python3 benchmark/compare.py [table] <before_label> <after_label>
  python3 benchmark/compare.py tools baseline bound
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
METRICS = [("mrr", "MRR"), ("recall_at_k", "Recall@k"),
           ("hit_at_1", "Hit@1"), ("hit_at_k", "Hit@k")]


def load(table, label):
    path = os.path.join(HERE, table, "results", f"{label}.json")
    if not os.path.exists(path):
        sys.exit(f"no result file: {path}")
    return json.load(open(path))


def arrow(d, eps=1e-9):
    return "→" if abs(d) < eps else ("▲" if d > 0 else "▼")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", default="tools")
    ap.add_argument("before")
    ap.add_argument("after")
    args = ap.parse_args()

    b = load(args.table, args.before)
    a = load(args.table, args.after)
    k = a.get("k", b.get("k"))

    print(f"\n{args.table}: {args.before} → {args.after}  (k={k})\n")
    # Strategy-level metric deltas
    strategies = sorted(set(b["strategies"]) | set(a["strategies"]))
    for key, lbl in METRICS:
        print(f"### {lbl}")
        print(f"| strategy | {args.before} | {args.after} | Δ |")
        print("| --- | --- | --- | --- |")
        for s in sorted(strategies,
                        key=lambda s: a["strategies"].get(s, {}).get(key, 0), reverse=True):
            bv = b["strategies"].get(s, {}).get(key)
            av = a["strategies"].get(s, {}).get(key)
            if bv is None or av is None:
                print(f"| {s} | {bv} | {av} | (missing) |")
                continue
            d = av - bv
            print(f"| {s} | {bv:.3f} | {av:.3f} | {arrow(d)} {d:+.3f} |")
        print()

    # Per-case rank movements, aggregated across strategies.
    # We compare first_rel_rank; None (not found in top-k) is treated as worse than any rank.
    print("### Per-case movements (first-relevant rank; None = not found in top-k)")
    print(f"| case | query | strategy | {args.before} | {args.after} |")
    print("| --- | --- | --- | --- | --- |")
    rank = lambda v: (10**9 if v is None else v)
    moved = []
    for s in strategies:
        bc = b.get("per_case", {}).get(s, {})
        ac = a.get("per_case", {}).get(s, {})
        for cid in sorted(set(bc) & set(ac)):
            br, ar = bc[cid]["first_rel_rank"], ac[cid]["first_rel_rank"]
            if rank(br) != rank(ar):
                moved.append((rank(ar) - rank(br), cid, ac[cid].get("query"), s, br, ar))
    # Improvements first (negative delta = better rank), then regressions
    for d, cid, q, s, br, ar in sorted(moved):
        tag = "▲" if d < 0 else "▼"
        fmt = lambda v: "—" if v is None else str(v)
        print(f"| {tag} {cid} | {q!r} | {s} | {fmt(br)} | {fmt(ar)} |")
    if not moved:
        print("| (no per-case rank changes) | | | | |")
    print()


if __name__ == "__main__":
    main()
