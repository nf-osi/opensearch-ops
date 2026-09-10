#!/usr/bin/env python3
"""Benchmark query strategies against a SearchIndex and golden relevance set.

Each case/strategy pair records ranked IDs and relevance metrics. Aggregates include:

  MRR        mean reciprocal rank of the first relevant hit
  Recall@k   mean fraction of relevant docs found in the top k
  Hit@1      fraction of cases whose #1 result is relevant
  Hit@k      fraction of cases with >=1 relevant doc in the top k
  rt_ms      mean round-trip time in ms (includes client polling)

Results are printed as Markdown and saved to `results/<label>.json`. Each result records
the dataset fingerprint, query configuration, and bound SearchConfiguration for comparison.

Benchmarks live in per-table subfolders: benchmark/<table>/{golden.yaml,
strategies.py, results/}. Pass the table as the first argument (default: tools).

Usage:
  python3 benchmark/run.py [table] [--label baseline] [--k 10] [--poll 0.15]
                           [--strategy multi_match_boosted ...]
  python3 benchmark/run.py tools --label kg-eval
"""
import argparse, hashlib, importlib.util, json, os, sys, time
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # repo root, for query.py
from query import search, hit_dict, _call  # noqa: E402


def load_strategies(table_dir):
    """Load table-specific strategies, falling back to shared strategies.

    The module provides `STRATEGIES` and `compile_production`.
    """
    for path in (os.path.join(table_dir, "strategies.py"),
                 os.path.join(HERE, "strategies.py")):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("strategies", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit(f"no strategies.py found in {table_dir} or {HERE}")


DEFAULT_CONTROL = "frontend_default"


def load_field_config(table_dir):
    """Load `fields.yaml` as `(fields, production, control, from_yaml)`.

    Fields retain their boosts; absent configuration defaults to `["*"]`. `production`,
    when present, defines `production_current`; `control` identifies the baseline strategy.
    """
    path = os.path.join(table_dir, "fields.yaml")
    if not os.path.exists(path):
        return ["*"], None, DEFAULT_CONTROL, False
    cfg = yaml.safe_load(open(path)) or {}
    return (cfg.get("fields") or ["*"],
            cfg.get("production"),
            cfg.get("control") or DEFAULT_CONTROL,
            True)


def golden_fingerprint(golden):
    """Return the golden version and a hash of score-relevant case content.

    The hash covers the ID field plus each case's ID, query, type, and sorted relevant IDs.
    It makes comparisons robust to unversioned content changes while excluding metadata
    unused by scoring. Index and `k` are recorded separately as run parameters.
    """
    cases = sorted(
        [{"id": c["id"], "query": c["query"], "type": c.get("type"),
          "relevant": sorted(c.get("relevant", []))} for c in golden.get("cases", [])],
        key=lambda c: c["id"])
    canon = json.dumps({"id_field": golden.get("id_field", "resourceId"), "cases": cases},
                       sort_keys=True, separators=(",", ":"))
    return {
        "version": golden.get("version"),
        "hash": hashlib.sha256(canon.encode()).hexdigest()[:12],
        "n_cases": len(cases),
    }


def field_config_fingerprint(fields, production, from_yaml):
    """Return the query configuration and its fingerprint.

    The fingerprint covers sorted fields and `production`; fields are retained in source
    order for display. `from_yaml` distinguishes an explicit configuration from the
    all-fields fallback.
    """
    canon = json.dumps({"fields": sorted(fields), "production": production},
                       sort_keys=True, separators=(",", ":"))
    return {
        "fields": fields,
        "production": production,
        "hash": hashlib.sha256(canon.encode()).hexdigest()[:12],
        "from_fields_yaml": from_yaml,
    }


def index_binding(index_id):
    """Return the SearchConfiguration bound to an index, if any.

    Index configuration is shared by all strategies and is recorded with each run.
    """
    try:
        code, ent = _call(f"entity/{index_id}")
        return ent.get("searchConfigurationId") if code < 400 else None
    except Exception:
        return None                          # never let a provenance read break a run


def warn_if_bound(index_id, strategies, config_id):
    """Warn if `frontend_default` is evaluated on a configured index.

    A platform-default measurement requires both the default query and no bound search
    configuration. Score it in a separate unbound run, then restore the configuration.
    """
    if not config_id or "frontend_default" not in strategies:
        return
    print(f"\n  WARNING: {index_id} has SearchConfiguration {config_id} bound, so "
          f"`frontend_default` here is\n"
          f"  the default QUERY on a CUSTOMIZED index — not the platform default. For a true\n"
          f"  platform-default number, unbind first (config.py unbind), score it under its own\n"
          f"  label, then re-bind — and keep that run as its own results file, since it measured\n"
          f"  a different index state (see search_config_id in each result). To publish that\n"
          f"  row without publishing the run, pin it with `constant:` in site.yaml.\n")


def warn_if_golden_drift(resdir, label, fp):
    """Warn when existing result files use a different golden fingerprint.

    Historical results remain valid, so this condition is informational rather than an
    error.
    """
    if not os.path.isdir(resdir):
        return
    stale = []
    for name in sorted(os.listdir(resdir)):
        if not name.endswith(".json") or name == f"{label}.json":
            continue
        try:
            other = json.load(open(os.path.join(resdir, name)))
        except (ValueError, OSError):
            continue                         # build_site.py is what reports unreadable runs
        og = other.get("golden") or {}
        if og.get("hash") != fp["hash"]:
            stale.append((name, og))
    if not stale:
        return
    rel = os.path.relpath(resdir, os.path.dirname(HERE))
    print(f"\n  WARNING: other runs in {rel}/ are not known to share this run's golden\n"
          f"  (this: {fp['version'] or 'unversioned'} / {fp['hash']}, {fp['n_cases']} cases), "
          f"so comparing them —\n"
          f"  here, in the published scoreboard, or in any hand-built table — is not\n"
          f"  apples-to-apples:")
    for name, og in stale:
        if og:
            print(f"    {name}: {og.get('version') or 'unversioned'} / {og.get('hash')} "
                  f"({og.get('n_cases')} cases)")
        else:
            print(f"    {name}: no dataset fingerprint (scored before run.py recorded one)")
    print("  Re-score each against the current golden under its own label, or keep it as a\n"
          "  historical record and read it as one.\n")


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
    """Return a hit identifier using the golden's configured ID field.

    `rowId` uses the index row identifier; other values name a hit column.
    """
    if id_field == "rowId":
        return hit.get("rowId")
    return hit_dict(hit).get(id_field)


def run(golden, strategies, fields, k, poll_s):
    index = golden["index"]
    id_field = golden.get("id_field", "resourceId")
    config_id = index_binding(index)
    warn_if_bound(index, strategies, config_id)
    out = {"index": index, "index_name": golden.get("index_name"), "k": k,
           "id_field": id_field,
           # which SearchConfiguration was bound while this run was scored (None = the
           # index was uncustomized). Without it a results file cannot say which index
           # state it measured, and two runs are not comparable unless this matches.
           "search_config_id": config_id,
           "strategies": {}, "per_case": {}}
    for sname, sfn in strategies.items():
        case_scores = []
        rts = []
        out["per_case"][sname] = {}
        for case in golden["cases"]:
            dsl = sfn(case["query"], max(k, 10), fields)
            t0 = time.time()
            res = search(index, dsl, response_parts=["HITS", "TOTAL_HITS"], poll_s=poll_s)
            rt = (time.time() - t0) * 1000.0
            ranked = [hit_id(h, id_field) for h in res.get("hits", [])]
            sc = score_case(ranked, case["relevant"], k)
            sc["rt_ms"] = rt
            sc["query"] = case["query"]
            sc["type"] = case.get("type")
            case_scores.append(sc)
            rts.append(rt)
            out["per_case"][sname][case["id"]] = sc
        n = len(case_scores)
        agg = {
            "mrr": sum(c["rr"] for c in case_scores) / n,
            "recall_at_k": sum(c["recall_at_k"] for c in case_scores) / n,
            "hit_at_1": sum(c["hit_at_1"] for c in case_scores) / n,
            "hit_at_k": sum(c["hit_at_k"] for c in case_scores) / n,
            "n_cases": n,
        }
        agg.update(rt_stats(rts))
        out["strategies"][sname] = agg
    return out


def rt_stats(rts):
    """Return round-trip latency statistics for a strategy.

    Prefer median and p95 when comparing strategies because polling and queueing can skew
    the mean.
    """
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", default="tools",
                    help="benchmark/<table>/ subfolder to run (default: tools)")
    ap.add_argument("--golden", default=None,
                    help="explicit golden path (overrides benchmark/<table>/golden.yaml)")
    ap.add_argument("--label", default="latest",
                    help="result filename stem -> results/<label>.json (default: latest)")
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--poll", type=float, default=0.15)
    ap.add_argument("--strategy", action="append", help="limit to named strategy/strategies")
    args = ap.parse_args()

    golden_path = args.golden or os.path.join(HERE, args.table, "golden.yaml")
    if not os.path.exists(golden_path):
        sys.exit(f"golden not found: {golden_path}")
    table_dir = os.path.dirname(os.path.abspath(golden_path))

    golden = yaml.safe_load(open(golden_path))
    k = args.k or golden.get("k", 10)
    strategy_mod = load_strategies(table_dir)
    all_strategies = dict(strategy_mod.STRATEGIES)
    fields, production, control, from_yaml = load_field_config(table_dir)
    if production is not None:
        all_strategies["production_current"] = strategy_mod.compile_production(production)
    if control not in all_strategies:
        sys.exit(f"fields.yaml names control {control!r}, which is not a strategy "
                 f"(have: {', '.join(sorted(all_strategies))})")
    strategies = all_strategies
    if args.strategy:
        unknown = [n for n in args.strategy if n not in all_strategies]
        if unknown:
            sys.exit(f"unknown strategy for {args.table}: {', '.join(unknown)} "
                     f"(have: {', '.join(sorted(all_strategies))}). Note production_current "
                     f"exists only for a table whose fields.yaml has a `production:` block.")
        # Always score the control, so every run can be read as a delta against it.
        wanted = list(dict.fromkeys(args.strategy + [control]))
        strategies = {n: all_strategies[n] for n in wanted}

    resdir = os.path.join(table_dir, "results")
    # Which dataset this run scores, checked against what is already in results/ BEFORE
    # scoring: a golden edit that makes this run incomparable to its siblings is worth
    # knowing now rather than after several hundred queries.
    gfp = golden_fingerprint(golden)
    fcfp = field_config_fingerprint(fields, production, from_yaml)
    if not gfp["version"]:
        print(f"\n  WARNING: {os.path.relpath(golden_path, os.path.dirname(HERE))} has no "
              f"`version:` — the run records only the content hash {gfp['hash']}.\n")
    warn_if_golden_drift(resdir, args.label, gfp)

    out = run(golden, strategies, fields, k, args.poll)
    out["control"] = control
    out["label"] = args.label
    out["run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # the dataset scored, not just how many cases it had: without this a results file
    # cannot say whether it is comparable to the one next to it (see golden_fingerprint)
    out["golden"] = gfp
    # …and the query-side config it scored them with, so the boosts a number came from stay
    # attached to it after fields.yaml moves on (see field_config_fingerprint)
    out["field_config"] = fcfp
    print(f"\nnf index: {out['index_name']} ({out['index']})  |  k={k}  |  "
          f"{gfp['n_cases']} cases  |  golden {gfp['version'] or 'unversioned'}/{gfp['hash']}"
          f"  |  boosts {fcfp['hash']}  |  label={args.label}\n")
    print(fmt_table(out))

    os.makedirs(resdir, exist_ok=True)
    path = os.path.join(resdir, f"{args.label}.json")
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
