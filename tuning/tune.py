#!/usr/bin/env python3
"""Standalone automated search-relevance tuning harness.

Given a benchmark table (its golden set + current fields.yaml under benchmark/<table>/), this
runs the full loop and recommends a better query-time config:

  1. Evaluate the seed candidates (today's hand-authored strategies) live -> baseline leaderboard.
  2. Probe every match field per case (cached) -> diagnostics + a free local re-ranker.
  3. Optimize the decomposable seeds' boosts numerically (zero live queries via the probe).
  4. Each round (unless --no-agent): ask Claude to propose new candidates (query types, field
     sets, starting boosts) from the profile + leaderboard + diagnostics; numerically tune each
     proposal's boosts; CONFIRM every optimized candidate with a real live query.
  5. Write the leaderboard, an experiment report, and the winning boosts as a drop-in fields.yaml.

Query-time only: it never touches the live index, analyzers, or SearchConfiguration. Apply a
winner by copying tuning/<table>/tuned_fields.yaml over benchmark/<table>/fields.yaml and
re-running benchmark/run.py.

Usage:
  python3 tuning/tune.py <table> [--rounds 3] [--candidates-per-round 6]
      [--objective ndcg|mrr|recall|hit1|hitk] [--k 10] [--model claude-opus-4-8]
      [--poll 0.15] [--no-agent]
"""
import argparse
import json
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmark"))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "generate-goldens"))

import candidate as C                              # noqa: E402
from evaluate import evaluate, objective_value, live_ranker, OBJECTIVES  # noqa: E402
from probe import probe, diagnose, is_local_rerankable  # noqa: E402
from optimize import optimize_boosts               # noqa: E402

BENCH = os.path.join(ROOT, "benchmark")


def load_table(table):
    gpath = os.path.join(BENCH, table, "golden.yaml")
    fpath = os.path.join(BENCH, table, "fields.yaml")
    if not os.path.exists(gpath):
        sys.exit(f"golden not found: {gpath}")
    golden = yaml.safe_load(open(gpath))
    fields_cfg = yaml.safe_load(open(fpath)) if os.path.exists(fpath) else {}
    fields_list = (fields_cfg or {}).get("fields") or ["*"]
    boosts = C.from_fields_yaml(fields_list)
    if not boosts:
        sys.exit(f"tuning needs an explicit benchmark/{table}/fields.yaml (the per-field probe "
                 f"can't run over a bare '*'). Add a fields: list first.")
    return golden, boosts


def golden_summary(golden, k):
    types = {}
    for c in golden["cases"]:
        types[c.get("type", "?")] = types.get(c.get("type", "?"), 0) + 1
    return {
        "n_cases": len(golden["cases"]), "k": k, "id_field": golden.get("id_field"),
        "index_name": golden.get("index_name"), "type_counts": types,
        "cases": [{"id": c["id"], "query": c["query"], "type": c.get("type"),
                   "n_relevant": len(c["relevant"])} for c in golden["cases"]],
    }


def lite(cand):
    """Candidate stripped to the fields that matter for the leaderboard / agent prompt."""
    return {k: cand.get(k) for k in ("name", "query_type", "multi_match_type", "fields",
                                     "fuzziness", "tie_breaker", "minimum_should_match",
                                     "phrase_boost") if cand.get(k) not in (None, {}, [])}


class Board:
    """Leaderboard keyed by candidate signature so duplicates collapse to their best score."""
    def __init__(self, objective):
        self.objective = objective
        self.entries = {}  # sig -> entry

    @staticmethod
    def _sig(cand):
        return json.dumps({k: cand.get(k) for k in
                           ("query_type", "multi_match_type", "fields", "fuzziness",
                            "tie_breaker", "minimum_should_match", "phrase_boost")},
                          sort_keys=True)

    def add(self, cand, ev, source):
        sig = self._sig(cand)
        obj = objective_value(ev["agg"], self.objective)
        cur = self.entries.get(sig)
        if cur is None or obj > cur["objective"]:
            self.entries[sig] = {"name": cand["name"], "source": source, "objective": obj,
                                 "agg": ev["agg"], "candidate": cand, "per_case": ev["per_case"]}
        return obj

    def ranked(self):
        return sorted(self.entries.values(), key=lambda e: e["objective"], reverse=True)

    def best(self):
        r = self.ranked()
        return r[0] if r else None

    def summary(self, top=8):
        return [{"name": e["name"], "source": e["source"],
                 "objective": round(e["objective"], 4),
                 "agg": {k: round(v, 4) for k, v in e["agg"].items() if k != "n_cases"},
                 "config": lite(e["candidate"])}
                for e in self.ranked()[:top]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", help="benchmark/<table>/ to tune (reads golden.yaml + fields.yaml)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--candidates-per-round", type=int, default=6)
    ap.add_argument("--objective", choices=OBJECTIVES, default="ndcg")
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--model", default="claude-opus-4-8")
    ap.add_argument("--poll", type=float, default=0.15)
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent index queries (queries are independent async jobs)")
    ap.add_argument("--max-cases", type=int, default=None,
                    help="use only the first N golden cases (fast smoke test)")
    ap.add_argument("--no-agent", action="store_true",
                    help="skip Claude proposals; just numerically optimize the current fields.yaml "
                         "boosts (no API key needed)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show progress even when piped
    except Exception:
        pass

    golden, boosts = load_table(args.table)
    if args.max_cases:
        golden["cases"] = golden["cases"][:args.max_cases]
    index = golden["index"]
    id_field = golden.get("id_field", "resourceId")
    k = args.k or golden.get("k", 10)
    allowed = sorted(boosts)
    outdir = os.path.join(HERE, args.table)
    os.makedirs(outdir, exist_ok=True)

    board = Board(args.objective)

    def confirm(cand):
        return evaluate(live_ranker(index, cand, id_field, args.poll), golden, k, args.workers)

    def live_score_fn(cand):
        return objective_value(confirm(cand)["agg"], args.objective)

    print(f"\ntuning {golden.get('index_name')} ({index})  table={args.table}  "
          f"objective={args.objective}  k={k}  {len(golden['cases'])} cases  "
          f"{len(allowed)} fields\n")

    # --- Round 0: seeds + probe + optimize decomposable seeds ------------------------------
    print("evaluating seed strategies live ...")
    seeds = C.seed_candidates(boosts)
    for s in seeds:
        obj = board.add(s, confirm(s), "seed")
        print(f"  {s['name']:22} {args.objective}={obj:.4f}")

    print("\nprobing fields (cached) ...")
    pm = probe(index, golden, allowed, id_field, args.poll,
               cache_path=os.path.join(outdir, "probe.json"), workers=args.workers)

    print("optimizing seed boosts via probe ...")
    for s in seeds:
        if is_local_rerankable(s):
            opt, _ = optimize_boosts(s, golden, pm, args.objective, k, allowed)
            opt = dict(opt); opt["name"] = s["name"] + "+opt"
            obj = board.add(opt, confirm(opt), "optimized")
            print(f"  {opt['name']:22} {args.objective}={obj:.4f}")

    # --- Agentic rounds --------------------------------------------------------------------
    if not args.no_agent:
        from propose import propose
        import profile_table
        print("\nprofiling index for the proposal agent ...")
        prof = profile_table.profile(index, 100, poll_s=args.poll)
        gsum = golden_summary(golden, k)
        best_obj = board.best()["objective"]
        stale = 0
        for r in range(args.rounds):
            print(f"\n=== round {r + 1}/{args.rounds} — asking {args.model} for "
                  f"{args.candidates_per_round} candidates ===")
            diags = diagnose(pm, golden)
            try:
                proposals = propose(prof, gsum, allowed, boosts, board.summary(),
                                    diags, n=args.candidates_per_round, model=args.model)
            except Exception as e:
                print(f"  proposal step failed ({e}); stopping rounds."); break
            for p in proposals:
                try:
                    p = C.normalize(p, allowed)
                except ValueError as e:
                    print(f"  skip {p.get('name')}: {e}"); continue
                opt, _ = optimize_boosts(p, golden, pm, args.objective, k, allowed,
                                         live_score_fn=live_score_fn)
                opt = dict(opt); opt["name"] = f"{p['name']}@r{r + 1}"
                obj = board.add(opt, confirm(opt), "proposed")
                rat = p.get("rationale", "")
                print(f"  {opt['name']:28} {args.objective}={obj:.4f}  {rat[:70]}")
            cur = board.best()["objective"]
            if cur > best_obj + 1e-6:
                best_obj, stale = cur, 0
            else:
                stale += 1
                if stale >= 2:
                    print("\nno improvement for 2 rounds — stopping early."); break

    write_outputs(args, golden, board, seeds, outdir, index, id_field, k)


def write_outputs(args, golden, board, seeds, outdir, index, id_field, k):
    best = board.best()
    ranked = board.ranked()

    # leaderboard.json
    json.dump({
        "index": index, "table": args.table, "objective": args.objective, "k": k,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "leaderboard": [{"name": e["name"], "source": e["source"],
                         "objective": e["objective"], "agg": e["agg"],
                         "config": lite(e["candidate"])} for e in ranked],
        "winner": lite(best["candidate"]),
    }, open(os.path.join(outdir, "leaderboard.json"), "w"), indent=2)

    # tuned_fields.yaml (winner's boosts, drop-in for benchmark/<table>/fields.yaml)
    win = best["candidate"]
    fy = ["# Auto-tuned field boosts (tuning/tune.py).",
          f"# index {golden.get('index_name')} ({index}); objective {args.objective}="
          f"{best['objective']:.4f}; winning query_type={win.get('query_type')}"
          + (f"/{win.get('multi_match_type')}" if win.get('multi_match_type') else ""),
          "# DRAFT — review, then copy over benchmark/<table>/fields.yaml and re-run run.py.",
          "fields:"]
    if win.get("fields"):
        fy += [f"  - \"{e}\"" for e in C.to_fields_yaml(win["fields"])]
    else:
        fy.append('  - "*"   # winner searched all fields (no explicit boosts)')
    open(os.path.join(outdir, "tuned_fields.yaml"), "w").write("\n".join(fy) + "\n")

    # report.md (experiment log — separate from RESULTS.md)
    md = report_md(args, golden, board, seeds, k)
    open(os.path.join(outdir, "report.md"), "w").write(md)

    print(f"\nwinner: {best['name']}  {args.objective}={best['objective']:.4f}")
    print(f"  {json.dumps(lite(win))}")
    print(f"\nwrote {outdir}/" + "{leaderboard.json, tuned_fields.yaml, report.md}")
    print("apply:  cp tuning/%s/tuned_fields.yaml benchmark/%s/fields.yaml  &&  "
          "python3 benchmark/run.py %s --label tuned" % (args.table, args.table, args.table))


def report_md(args, golden, board, seeds, k):
    obj = args.objective
    ranked = board.ranked()
    best = ranked[0]
    base = next((e for e in board.entries.values()
                 if e["candidate"].get("name") == "frontend_default"), None)
    L = [f"# Search tuning report — {golden.get('index_name')} ({golden['index']})", "",
         f"Objective: **{obj}@{k}**.  {len(golden['cases'])} golden cases.  "
         f"Run {time.strftime('%Y-%m-%d', time.gmtime())}.", "",
         "Query-time tuning only (query type + field selection + boosts). The winner's boosts "
         "are written to `tuned_fields.yaml`; copy over `benchmark/<table>/fields.yaml` and "
         "re-run `benchmark/run.py` to confirm on the live index.", "",
         "## Leaderboard", "",
         f"| config | source | {obj} | MRR | Recall@{k} | Hit@1 | nDCG@{k} |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for e in ranked[:12]:
        a = e["agg"]
        L.append(f"| {e['name']} | {e['source']} | {e['objective']:.3f} | {a['mrr']:.3f} | "
                 f"{a['recall_at_k']:.3f} | {a['hit_at_1']:.3f} | {a['ndcg_at_k']:.3f} |")
    L += ["", "## Winner", "", "```json", json.dumps(lite(best["candidate"]), indent=2), "```", ""]
    if base:
        d = best["objective"] - objective_value(base["agg"], obj)
        L.append(f"vs the production baseline (`frontend_default`): {obj} "
                 f"{objective_value(base['agg'], obj):.3f} → {best['objective']:.3f} "
                 f"({'+' if d >= 0 else ''}{d:.3f}).")
        L.append("")
        L += ["## Per-case (winner vs frontend_default)", "",
              f"| case | query | base {obj} | winner {obj} |", "| --- | --- | --- | --- |"]
        bk = "ndcg_at_k" if obj == "ndcg" else {"mrr": "rr", "recall": "recall_at_k",
                                                "hit1": "hit_at_1", "hitk": "hit_at_k"}[obj]
        for c in golden["cases"]:
            bp = base["per_case"].get(c["id"], {}).get(bk, 0.0)
            wp = best["per_case"].get(c["id"], {}).get(bk, 0.0)
            mark = " ⬆" if wp > bp + 1e-6 else (" ⬇" if wp < bp - 1e-6 else "")
            L.append(f"| {c['id']} | {c['query']} | {bp:.3f} | {wp:.3f}{mark} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
