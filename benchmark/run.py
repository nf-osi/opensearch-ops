#!/usr/bin/env python3
"""Benchmark query strategies against a SearchIndex using a golden relevance set.

For every (case x strategy) it runs the query, records the ranked resourceIds,
and scores them against the case's relevant set. Per-strategy aggregates:

  MRR        mean reciprocal rank of the first relevant hit
  Recall@k   mean fraction of relevant docs found in the top k
  Hit@1      fraction of cases whose #1 result is relevant
  Hit@k      fraction of cases with >=1 relevant doc in the top k
  rt_ms      mean round-trip ms (includes client poll interval; rough)

Results are printed as a markdown table and written to results/<label>.json so
runs can be diffed as the index config (analyzers, synonyms, boosts) changes.

Benchmarks live in per-table subfolders: benchmark/<table>/{golden.yaml,
strategies.py, results/}. Pass the table as the first argument (default: tools).

Usage:
  python3 benchmark/run.py [table] [--label baseline] [--k 10] [--poll 0.15]
                           [--strategy multi_match_boosted ...]
  python3 benchmark/run.py tools --label kg-eval
  python3 benchmark/run.py tools --case pnf   # run one golden case to spot-check the live index
"""
import argparse, importlib.util, json, os, sys, time
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # repo root, for query.py
from query import search, hit_dict  # noqa: E402


def load_strategies(table_dir):
    """Load STRATEGIES from the table's strategies.py, falling back to the
    shared benchmark/strategies.py if the table doesn't define its own."""
    for path in (os.path.join(table_dir, "strategies.py"),
                 os.path.join(HERE, "strategies.py")):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("strategies", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.STRATEGIES
    sys.exit(f"no strategies.py found in {table_dir} or {HERE}")


def load_fields(table_dir):
    """Load the table's match fields (with `^N` boosts) from benchmark/<table>/fields.yaml.

    Returns the list as-is, e.g. ["resourceName^5", ...]; strategies that want unboosted
    matching strip the `^weight` themselves. Falls back to ["*"] (all fields, no boost)
    when the table has no fields.yaml."""
    path = os.path.join(table_dir, "fields.yaml")
    if os.path.exists(path):
        cfg = yaml.safe_load(open(path)) or {}
        return cfg.get("fields") or ["*"]
    return ["*"]


def reciprocal_rank(ranked_ids, relevant):
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in relevant:
            return 1.0 / i, i
    return 0.0, None


def score_case(ranked_ids, relevant, k):
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


def hit_id(hit, id_field):
    """The stable identifier for a hit, per the golden's id_field.

    `id_field` is the column whose value the golden's `relevant` ids come from
    (e.g. resourceId for nf-tools; varies by portal/table). Use "rowId" to key on
    the index's own per-row id, which is always present and needs no column.
    """
    if id_field == "rowId":
        return hit.get("rowId")
    return hit_dict(hit).get(id_field)


def run(golden, strategies, fields, k, poll_s, workers=1):
    index = golden["index"]
    id_field = golden.get("id_field", "resourceId")
    out = {"index": index, "index_name": golden.get("index_name"), "k": k,
           "id_field": id_field, "workers": workers, "strategies": {}, "per_case": {}}

    def run_one(sname, sfn, case):
        dsl = sfn(case["query"], max(k, 10), fields)
        t0 = time.time()
        res = search(index, dsl, response_parts=["HITS", "TOTAL_HITS"], poll_s=poll_s)
        rt = (time.time() - t0) * 1000.0
        ranked = [hit_id(h, id_field) for h in res.get("hits", [])]
        sc = score_case(ranked, case["relevant"], k)
        sc["rt_ms"] = rt
        sc["query"] = case["query"]
        sc["type"] = case.get("type")
        return sname, case["id"], sc

    tasks = [(sname, sfn, case) for sname, sfn in strategies.items()
             for case in golden["cases"]]
    for sname in strategies:
        out["per_case"][sname] = {}

    # Queries are independent and I/O-bound (async start + poll), so a thread pool
    # gives a near-linear speedup until the server-side job queue saturates. workers=1
    # keeps the sequential path so rt_ms stays a clean per-query latency; >1 trades
    # latency fidelity (queries then contend) for wall-clock on scoring runs.
    if workers <= 1:
        results = [run_one(*t) for t in tasks]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda t: run_one(*t), tasks))

    for sname, cid, sc in results:
        out["per_case"][sname][cid] = sc

    for sname in strategies:
        case_scores = list(out["per_case"][sname].values())
        n = len(case_scores)
        agg = {
            "mrr": sum(c["rr"] for c in case_scores) / n,
            "recall_at_k": sum(c["recall_at_k"] for c in case_scores) / n,
            "hit_at_1": sum(c["hit_at_1"] for c in case_scores) / n,
            "hit_at_k": sum(c["hit_at_k"] for c in case_scores) / n,
            "n_cases": n,
        }
        agg.update(rt_stats([c["rt_ms"] for c in case_scores]))
        out["strategies"][sname] = agg
    return out


def rt_stats(rts):
    """Round-trip latency spread for a strategy. Median/p95 are more meaningful than
    mean here: each rt includes the client poll interval and time queued behind other
    async jobs, so the mean is skewed by occasional stalls. Compare strategies on the
    median; watch p95 for tail cost (e.g. fuzzy/cross fanning out)."""
    s = sorted(rts)
    n = len(s)
    if not n:
        return {"rt_ms_mean": 0.0, "rt_ms_median": 0.0, "rt_ms_min": 0.0,
                "rt_ms_max": 0.0, "rt_ms_p95": 0.0}
    pct = lambda p: s[min(n - 1, int(round((p / 100.0) * (n - 1))))]
    return {
        "rt_ms_mean": sum(s) / n,
        "rt_ms_median": pct(50),
        "rt_ms_min": s[0],
        "rt_ms_max": s[-1],
        "rt_ms_p95": pct(95),
    }


def fmt_table(out):
    k = out["k"]
    rows = sorted(out["strategies"].items(), key=lambda kv: kv[1]["mrr"], reverse=True)
    hdr = f"| strategy | MRR | Recall@{k} | Hit@1 | Hit@{k} | rt_ms_med | rt_ms_p95 |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    lines = [hdr, sep]
    for name, a in rows:
        lines.append(f"| {name} | {a['mrr']:.3f} | {a['recall_at_k']:.3f} | "
                     f"{a['hit_at_1']:.3f} | {a['hit_at_k']:.3f} | "
                     f"{a['rt_ms_median']:.0f} | {a['rt_ms_p95']:.0f} |")
    return "\n".join(lines)


def fmt_per_case(out):
    """Per-(case x strategy) ranks. Used for targeted spot checks (--case): shows
    where the relevant docs landed, so you can confirm a config change took effect."""
    k = out["k"]
    lines = [f"| case | strategy | first_rel_rank | found@{k}/rel | Hit@1 |",
             "| --- | --- | --- | --- | --- |"]
    for sname, cases in out["per_case"].items():
        for cid, sc in cases.items():
            rank = sc["first_rel_rank"] if sc["first_rel_rank"] is not None else "—"
            lines.append(f"| {cid} ({sc['query']!r}) | {sname} | {rank} | "
                         f"{sc['n_found_in_k']}/{sc['n_relevant']} | "
                         f"{'yes' if sc['hit_at_1'] else 'no'} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", default="tools",
                    help="benchmark/<table>/ subfolder to run (default: tools)")
    ap.add_argument("--golden", default=None,
                    help="explicit golden path (overrides benchmark/<table>/golden.yaml)")
    ap.add_argument("--label", default=None,
                    help="result filename stem -> results/<label>.json (default: latest; "
                         "a --case spot-check skips the file write unless you pass --label)")
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--poll", type=float, default=0.15)
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent queries (default 1 = sequential, clean rt_ms). "
                         ">1 fans out via a thread pool for a big wall-clock win on scoring "
                         "runs, but rt_ms then reflects contended throughput, not latency.")
    ap.add_argument("--strategy", action="append", help="limit to named strategy/strategies")
    ap.add_argument("--case", action="append",
                    help="limit to golden case id(s); prints per-case ranks. Handy for "
                         "spot-checking the live index after a config change (e.g. --case pnf).")
    args = ap.parse_args()

    golden_path = args.golden or os.path.join(HERE, args.table, "golden.yaml")
    if not os.path.exists(golden_path):
        sys.exit(f"golden not found: {golden_path}")
    table_dir = os.path.dirname(os.path.abspath(golden_path))

    golden = yaml.safe_load(open(golden_path))
    if args.case:
        want = set(args.case)
        golden["cases"] = [c for c in golden["cases"] if c["id"] in want]
        missing = want - {c["id"] for c in golden["cases"]}
        if missing:
            sys.exit(f"no such case id(s) in golden: {sorted(missing)}")
    k = args.k or golden.get("k", 10)
    all_strategies = load_strategies(table_dir)
    strategies = all_strategies
    if args.strategy:
        strategies = {n: all_strategies[n] for n in args.strategy}
    fields = load_fields(table_dir)

    out = run(golden, strategies, fields, k, args.poll, args.workers)
    # A --case spot-check skips the file write (so it can't clobber results/latest.json)
    # unless the user names an explicit --label.
    label = args.label or ("latest" if not args.case else None)
    out["label"] = label
    out["run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"\nnf index: {out['index_name']} ({out['index']})  |  k={k}  |  "
          f"{len(golden['cases'])} cases  |  label={label}\n")
    print(fmt_table(out))
    if args.case:
        print()
        print(fmt_per_case(out))

    if label is None:
        print("\n(spot-check: no results file written; pass --label to persist)")
        return
    resdir = os.path.join(table_dir, "results")
    os.makedirs(resdir, exist_ok=True)
    path = os.path.join(resdir, f"{label}.json")
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
