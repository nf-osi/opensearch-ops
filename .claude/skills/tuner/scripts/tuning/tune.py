#!/usr/bin/env python3
"""Standalone automated search-relevance tuning harness.

Given a benchmark table (its golden set under benchmark/<table>/), this recommends a better
query-time config. An existing fields.yaml (today's hand-tuned strategy) is optional — 
if none exists, one is bootstrapped by profiling the live index (see
`load_table`), so there's always a real field list to explore, not just a bare `*`:

  1. Probe every match field per case (cached) -> diagnostics + a free local re-ranker. First,
     so the priciest step is on disk before anything else and a killed run resumes cheaply.
  2. Evaluate the seed candidates (today's hand-authored strategies) live -> baseline leaderboard.
  3. Optimize the decomposable seeds' boosts numerically (zero live queries via the probe).
  4. Each round, new candidates (query types, field sets, starting boosts) get their boosts
     numerically tuned; every optimized candidate is CONFIRMED with a real live query.
  5. Write the leaderboard, an experiment report, and the winning config (boosts + query
     shape) as a drop-in fields.yaml.

Query-time only: it never touches the live index, analyzers, or SearchConfiguration. Apply a
winner by copying <out>/<table>/tuned_fields.yaml over benchmark/<table>/fields.yaml and
re-running benchmark/run.py.

The agentic step (round 4 above) is driven by the CALLING agent — it reads
round_context.json, authors candidates.json itself (matching candidate.AGENT_CANDIDATE_SCHEMA),
and drives the rounds via `init` / `add-candidates` / `finalize`. No nested Anthropic API call
and no ANTHROPIC_API_KEY are involved anywhere in this harness:

    python3 tune.py init <table> --bench <DIR> --out <DIR> [--objective ndcg] [--max-cases N]
    # agent reads <out>/<table>/round_context.json, writes candidates.json
    python3 tune.py add-candidates <table> candidates.json --bench <DIR> --out <DIR>
    # repeat add-candidates for more rounds, then:
    python3 tune.py finalize <table> --out <DIR>

--bench/--out say where the golden set is read from and where artifacts are written. Omit
them on a local checkout; pass them in a sandbox, where this code is mounted read-only and
cannot write beside itself.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import yaml

# Every module this harness needs is a sibling of this file — no repo tree to discover, no
# scripts borrowed from another skill — so the same files run unchanged from a repo checkout
# and from a read-only skill bundle.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import candidate as C                              # noqa: E402
from client import KEYWORD_TYPES, column_types, is_text_searchable   # noqa: E402
from evaluate import (evaluate, objective_value, live_ranker,      # noqa: E402
                      OBJECTIVES, OBJ_CASE_KEY)
from index_profile import profile as profile_index  # noqa: E402
from probe import (probe, diagnose, is_local_rerankable, profile_brief,   # noqa: E402
                   SAVE_EVERY)
from optimize import optimize_boosts, probe_score_fn, saturation   # noqa: E402

# WHERE data is read from and written to, kept separate from where this code lives — the code
# may sit in a READ-ONLY Agent Skill bundle, so it can never assume it can write beside itself.
#
#   BENCH  reads  <BENCH>/<table>/{golden,fields}.yaml
#   RUNS   writes <RUNS>/<table>/{probe.json,state.json,round_context.json,leaderboard.json,
#                                tuned_fields.yaml,report.md}
#
# RUNS artifacts are working files, not deliverables: a probe cache, resumable state, and the
# three outputs the caller collects. They land in a plain ./tuner-runs/ beside wherever you
# invoke from, and are gitignored — nothing in the repo tree is expected to hold them.
#
# resolve_paths() rebinds these per invocation from --bench/--out; these module-level values
# are only a placeholder. Precedence: explicit flag > $TUNER_WORK_DIR > the working directory
# the command was launched from (the repo root, on a local checkout).
RUNS_DIRNAME = "tuner-runs"        # <cwd>/tuner-runs/<table>/ unless --out says otherwise

BENCH = os.path.join(os.getcwd(), "benchmark")
RUNS = os.path.join(os.getcwd(), RUNS_DIRNAME)


def resolve_paths(args):
    """Bind BENCH/RUNS for this invocation.

    Prefer the explicit flags over $TUNER_WORK_DIR, especially in an agent sandbox: each bash
    tool call can be a fresh shell, so an `export` may silently fail to carry over between
    commands and the harness would quietly fall back to somewhere unwritable. A flag on the
    command line cannot go missing that way.

    The last resort is the CWD, not this file's location: the code may be installed anywhere
    (a read-only bundle), so where it happens to sit says nothing about where the data is."""
    global BENCH, RUNS
    work = os.environ.get("TUNER_WORK_DIR") or os.getcwd()
    bench, out = getattr(args, "bench", None), getattr(args, "out", None)
    BENCH = os.path.abspath(bench) if bench else os.path.join(work, "benchmark")
    RUNS = os.path.abspath(out) if out else os.path.join(work, RUNS_DIRNAME)


def run_dir(table):
    """Per-table output directory. Never inside this script's own directory — that is the
    read-only skill bundle in a Managed Agent session."""
    return os.path.join(RUNS, table)

# Hard ceilings on the agentic loop. The agent-driven init/add-candidates path otherwise has
# no built-in stopping point besides the agent's own judgment (round_context.json's
# stale_rounds signal is only a recommendation) — and each round issues real live Synapse
# queries (see README's "Query volume" note), so an unattended or confused agent could keep
# proposing indefinitely. These are enforced by the harness itself, not just documented:
# add-candidates refuses a round past MAX_ROUNDS, and truncates any batch bigger than
# MAX_CANDIDATES_PER_ROUND.
MAX_ROUNDS = 8
MAX_CANDIDATES_PER_ROUND = 10


class _BlockDumper(yaml.SafeDumper):
    """SafeDumper that indents list items under their key. Purely cosmetic — PyYAML's default
    puts a nested sequence at the same column as its parent key, which is valid but reads badly
    in tuned_fields.yaml, a file a human is expected to review before applying."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def load_table(table, poll=0.15):
    """Load golden.yaml + fields.yaml for a table. If fields.yaml doesn't exist (or is a bare
    `*`, i.e. no explicit fields list), there's no existing search strategy to compare
    against — bootstrap a starting {field: boost} map instead of requiring one be provided,
    by profiling the live index (name/category/text columns; identifiers excluded).

    Either way the field list is then checked against the index's real column types and any
    field a free-text query cannot touch (DATE, INTEGER, …) is dropped with a printed note.
    This is not tidiness: querying such a column 500s, which used to kill the whole run partway
    through. A hand-written fields.yaml naming one is just as fatal as a bootstrapped list, so
    the check applies to both.

    Returns (golden, boosts, bootstrapped, types) — `bootstrapped` is True when the field list
    was inferred rather than read from an existing fields.yaml; `types` is {column: columnType}
    for the live index (empty if it has no `index` set yet)."""
    gpath = os.path.join(BENCH, table, "golden.yaml")
    fpath = os.path.join(BENCH, table, "fields.yaml")
    if not os.path.exists(gpath):
        sys.exit(f"golden not found: {gpath}")
    golden = yaml.safe_load(open(gpath))
    boosts = {}
    if os.path.exists(fpath):
        fields_cfg = yaml.safe_load(open(fpath)) or {}
        boosts = C.from_fields_yaml(fields_cfg.get("fields") or ["*"])

    index = golden.get("index")
    if boosts:
        if not index:
            return golden, boosts, False, {}
        types = column_types(index, poll_s=poll)
        boosts = _drop_unsearchable(boosts, types, f"benchmark/{table}/fields.yaml")
        if not boosts:
            sys.exit(f"every field in benchmark/{table}/fields.yaml is a non-text column — "
                     f"nothing left to search. Fix the field list.")
        return golden, boosts, False, types

    if not index:
        sys.exit(f"no fields.yaml for {table}, and golden.yaml has no `index` set yet — can't "
                 f"profile a table that doesn't exist to bootstrap one. Add a fields.yaml, or "
                 f"set golden.yaml's `index` once the SearchIndex is built.")
    print(f"no fields.yaml for {table} — bootstrapping a starting field set by profiling {index} ...")
    prof = profile_index(index, 100, poll_s=poll)
    boosts = C.bootstrap_boosts(prof)
    if not boosts:
        sys.exit(f"couldn't infer any searchable fields from {index}'s schema (no name/category/"
                 f"text columns found) — provide an explicit benchmark/{table}/fields.yaml.")
    print(f"  bootstrapped {len(boosts)} fields: {sorted(boosts)}")
    return golden, boosts, True, prof.get("types") or {}


def _drop_unsearchable(boosts, types, source):
    """Drop fields whose column type no free-text query can match, with a printed note naming
    them. Unknown columns are kept — a name absent from `types` may be a real field the index
    just didn't report, and letting the index reject it is better than silently ignoring it."""
    bad = {f: types.get(f) for f in boosts
           if f in types and not is_text_searchable(types[f])}
    if not bad:
        return boosts
    print(f"  dropping {len(bad)} non-text field(s) from {source} — a free-text query against "
          f"these is rejected by the index: " +
          ", ".join(f"{f} ({t})" for f, t in sorted(bad.items())))
    return {f: b for f, b in boosts.items() if f not in bad}


def phrase_unsafe_fields(types):
    """Keyword-mapped columns from a {column: columnType} map. A phrase / phrase_prefix query
    against one of these is rejected by the index (they are searchable for term queries, so
    they stay in the field list — only the phrase-shaped candidates must avoid them)."""
    return {f for f, t in (types or {}).items() if t in KEYWORD_TYPES}


def sliced_golden(golden, max_cases):
    """A shallow copy of `golden` restricted to its first `max_cases` cases (the --max-cases
    tuning slice), or `golden` unchanged when max_cases is falsy or already covers every case.
    Never mutates the input, so the full set stays available for the final winner verification."""
    if not max_cases or max_cases >= len(golden["cases"]):
        return golden
    g = dict(golden)
    g["cases"] = golden["cases"][:max_cases]
    return g


def golden_summary(golden, k, n_cases_total=None):
    types = {}
    for c in golden["cases"]:
        types[c.get("type", "?")] = types.get(c.get("type", "?"), 0) + 1
    n_used = len(golden["cases"])
    return {
        "n_cases": n_used, "n_cases_total": n_cases_total if n_cases_total is not None else n_used,
        "is_slice": n_cases_total is not None and n_used < n_cases_total,
        "k": k, "id_field": golden.get("id_field"),
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
    def __init__(self, objective, entries=None):
        self.objective = objective
        self.entries = entries if entries is not None else {}  # sig -> entry

    @staticmethod
    def _sig(cand):
        return json.dumps({k: cand.get(k) for k in
                           ("query_type", "multi_match_type", "fields", "fuzziness",
                            "tie_breaker", "minimum_should_match", "phrase_boost")},
                          sort_keys=True)

    def add(self, cand, ev, source, sat=None):
        sig = self._sig(cand)
        obj = objective_value(ev["agg"], self.objective)
        cur = self.entries.get(sig)
        if cur is None or obj > cur["objective"]:
            self.entries[sig] = {"name": cand["name"], "source": source, "objective": obj,
                                 "agg": ev["agg"], "candidate": cand, "per_case": ev["per_case"],
                                 # boosts left pinned at the optimizer's ceiling, if any — see
                                 # optimize.saturation(). Computed only where it's free.
                                 "saturation": sat or []}
        return obj

    def has(self, cand):
        """Already scored? Keyed by config signature, not name — so a resumed run skips the
        real work rather than re-scoring an identical config under a different label."""
        return self._sig(cand) in self.entries

    def entry(self, cand):
        return self.entries.get(self._sig(cand))

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


# --- shared by `init` / `add-candidates` / `finalize` --------------------------------------

def _diagnose_vs_winner(pm, golden, board, objective):
    """diagnose() through the lens of the current leaderboard winner — its field weighting for
    the distractor analysis, its live per-case scores for the worst-first ordering — so the
    signal the agent reads actually shifts round to round as the winner improves."""
    best = board.best()
    return diagnose(pm, golden, winner=best["candidate"] if best else None,
                    winner_per_case=best["per_case"] if best else None, objective=objective)


def _saturation_if_free(cand, golden, pm, objective, k):
    """optimize.saturation() for `cand`, but only when the probe can score it for free.

    Non-decomposable candidates are skipped rather than charged: each extra config is another
    `cases` live queries, and this is a diagnostic, not part of the recommendation."""
    if not is_local_rerankable(cand):
        return []
    return saturation(cand, probe_score_fn(golden, pm, objective, k))


def _print_saturation(sat):
    """One line per pinned boost. Worth surfacing during the round, not just at finalize: it's
    the clearest available signal that a config is fitting the golden set rather than the data."""
    for s in sat:
        if s["flat"]:
            print(f"      note: {s['field']} pinned at {s['boost']} but halving it changes "
                  f"nothing ({s['delta']:+.4f}) — the value is arbitrary, prefer the smaller one")
        else:
            print(f"      note: {s['field']} pinned at {s['boost']}; halving it costs "
                  f"{s['delta']:+.4f} — ranking hinges on this one field (brittle on few cases)")


def _process_candidates(raw_candidates, board, golden, pm, objective, k, allowed, round_num,
                        confirm, live_score_fn, on_committed=None, phrase_unsafe=()):
    """Per-candidate pipeline: for each agent-schema candidate, convert → normalize/validate →
    numerically optimize its boosts → confirm live → add to the
    board under a round-tagged name. Skips (with a printed note) any candidate that normalizes
    to nothing usable. Returns the number of candidates that made it onto the board.

    `on_committed(board)` — if given — is invoked right after each candidate lands on the board,
    so the caller can checkpoint incrementally. Each candidate is minutes of live queries, so a
    kill/timeout mid-round would otherwise discard every candidate completed so far."""
    added = 0
    for c in raw_candidates:
        try:
            p = C.normalize(C.to_candidate(c), allowed, phrase_unsafe=phrase_unsafe)
        except ValueError as e:
            print(f"  skip {c.get('name')}: {e}")
            continue
        # Announce before the slow optimize+confirm so a long-running (live-optimized) candidate
        # is distinguishable from a hang. Decomposable candidates tune for free off the probe;
        # the rest are scored with live queries (~5x slower) — say which so the wait makes sense.
        path = "probe" if is_local_rerankable(p) else "live (slower)"
        print(f"  · {p['name']:26} optimizing [{path}] ...", flush=True)
        opt, _ = optimize_boosts(p, golden, pm, objective, k, allowed, live_score_fn=live_score_fn)
        opt = dict(opt); opt["name"] = f"{p['name']}@r{round_num}"
        sat = _saturation_if_free(opt, golden, pm, objective, k)
        obj = board.add(opt, confirm(opt), "proposed", sat=sat)
        added += 1
        print(f"    {opt['name']:28} {objective}={obj:.4f}  {c.get('rationale', '')[:70]}")
        _print_saturation(sat)
        if on_committed is not None:
            on_committed(board)
    return added


def _seed_and_probe(table, golden, boosts, objective, k, poll, workers, outdir,
                    bootstrapped=False, types=None, resume=None, on_progress=None):
    """Round 0: probe fields, evaluate seeds live, optimize decomposable seeds. Returns
    (board, pm, index, id_field, baseline_ev) — baseline_ev is `frontend_default`'s
    own {agg, per_case} evaluation, captured directly (not looked up in the board afterward)
    so the vs-default comparison in the report is guaranteed even in the — currently
    theoretical, since no other seed/proposal shares its exact signature — case where some
    other candidate collapses onto the same leaderboard signature and displaces it.
    `bootstrapped` just controls a header note — it doesn't change scoring.

    RESUMABLE. Round 0 is the longest unattended stretch in the harness (every seed is `cases`
    live queries and the probe is `cases × fields`), so a crash or timeout partway used to
    throw away all of it. Two mechanisms:

      - the probe runs FIRST and writes its cache before any seed is scored, so the most
        expensive single step is never repeated;
      - `resume` is a previous run's {board_entries, baseline_ev}; any seed already on that
        board is skipped, and `on_progress(board, baseline_ev)` is called after each seed so
        the caller can persist incrementally.

    Ordering the probe first also means an unsearchable field is caught before ~7 seeds' worth
    of live queries, not after."""
    index = golden["index"]
    id_field = golden.get("id_field", "resourceId")
    allowed = sorted(boosts)
    board = Board(objective, entries=dict((resume or {}).get("board_entries") or {}))
    baseline_ev = (resume or {}).get("baseline_ev")

    def confirm(cand):
        return evaluate(live_ranker(index, cand, id_field, poll), golden, k, workers)

    def commit(cand, ev, source, sat=None):
        obj = board.add(cand, ev, source, sat=sat)
        if on_progress is not None:
            on_progress(board, baseline_ev)
        return obj

    fields_note = " (bootstrapped from index schema, no existing fields.yaml)" if bootstrapped else ""
    print(f"\ntuning {golden.get('index_name')} ({index})  table={table}  "
          f"objective={objective}  k={k}  {len(golden['cases'])} cases  "
          f"{len(allowed)} fields{fields_note}\n")

    # Probe first: it's the single most expensive step and the one that caches, so getting it
    # on disk before anything else is what makes a killed init cheap to resume.
    print(f"probing {len(allowed)} fields × {len(golden['cases'])} cases (cached) ...", flush=True)

    def probe_progress(done, total):
        # A silent multi-minute phase reads as a hang. Report on the same cadence the partial
        # cache is flushed, so the number on screen matches what's actually recoverable.
        if done % SAVE_EVERY == 0 or done == total:
            print(f"  {done}/{total} queries", flush=True)

    pm = probe(index, golden, allowed, id_field, poll,
               cache_path=os.path.join(outdir, "probe.json"), workers=workers,
               progress=probe_progress)

    phrase_unsafe = phrase_unsafe_fields(types)
    seeds = C.seed_candidates(boosts, phrase_unsafe=phrase_unsafe)
    if phrase_unsafe & set(boosts):
        print(f"  (excluding keyword-mapped field(s) from phrase queries: "
              f"{sorted(phrase_unsafe & set(boosts))})")
    n_done = sum(1 for s in seeds if board.has(s))
    print(f"\nevaluating {len(seeds) - n_done} seed strategies live"
          + (f" ({n_done} already scored — resuming)" if n_done else "") + " ...", flush=True)
    for s in seeds:
        if board.has(s):
            print(f"  {s['name']:22} (cached)")
            if s["name"] == "frontend_default" and baseline_ev is None:
                # board entries don't distinguish which seed produced them by name alone, but
                # the signature does — recover the baseline from it rather than re-querying.
                baseline_ev = board.entry(s)
            continue
        ev = confirm(s)
        if s["name"] == "frontend_default":
            baseline_ev = ev
        obj = commit(s, ev, "seed")
        print(f"  {s['name']:22} {objective}={obj:.4f}", flush=True)

    print("\noptimizing seed boosts via probe ...", flush=True)
    for s in seeds:
        if is_local_rerankable(s):
            opt, _ = optimize_boosts(s, golden, pm, objective, k, allowed)
            opt = dict(opt); opt["name"] = s["name"] + "+opt"
            if board.has(opt):
                print(f"  {opt['name']:22} (cached)")
                continue
            sat = _saturation_if_free(opt, golden, pm, objective, k)
            obj = commit(opt, confirm(opt), "optimized", sat=sat)
            print(f"  {opt['name']:22} {objective}={obj:.4f}", flush=True)
            _print_saturation(sat)

    return board, pm, index, id_field, baseline_ev


def verify_full(objective, golden_full, board, index, id_field, k, boosts, poll, workers,
                max_cases):
    """Re-score the winner and the `frontend_default` baseline on the FULL golden set with live
    queries. The leaderboard's numbers are slice scores when --max-cases was used; this is the
    authoritative full-set check the recommendation must stand on.

    Returns a dict with full-set winner/baseline aggs + per-case, and the case counts — or None
    when tuning already covered every case (no slice), in which case the board numbers already
    are full-set numbers and no re-verification is needed."""
    n_total = len(golden_full["cases"])
    n_used = len(sliced_golden(golden_full, max_cases)["cases"])
    if n_used >= n_total:
        return None

    def confirm_full(cand):
        return evaluate(live_ranker(index, cand, id_field, poll), golden_full, k, workers)

    print(f"\nverifying winner + baseline on the full {n_total}-case golden set "
          f"(rounds tuned on {n_used}) ...")
    winner_full = confirm_full(board.best()["candidate"])
    baseline_full = confirm_full(C.seed_candidates(boosts)[0])   # seed 0 == frontend_default
    # frontend_default carries no field list, so no phrase_unsafe filtering is needed here.
    return {
        "n_cases_used": n_used, "n_cases_total": n_total,
        "winner_agg": winner_full["agg"], "winner_per_case": winner_full["per_case"],
        "baseline_agg": baseline_full["agg"], "baseline_per_case": baseline_full["per_case"],
        # full case list (id + query) so the report's per-case table can cover every case, not
        # just the slice the leaderboard was scored on.
        "cases": [{"id": c["id"], "query": c["query"]} for c in golden_full["cases"]],
    }


def write_outputs(table, objective, golden, board, outdir, index, id_field, k, baseline_ev,
                  bootstrapped=False, verification=None):
    best = board.best()
    ranked = board.ranked()

    # Slice-aware scoring: when --max-cases was used, the board holds SLICE scores and
    # `verification` holds the authoritative full-set re-scores of the winner + baseline.
    win_obj = best["objective"]
    win_agg = best["agg"]
    base_agg = baseline_ev["agg"] if baseline_ev else None
    if verification:
        win_obj = objective_value(verification["winner_agg"], objective)
        win_agg = verification["winner_agg"]
        base_agg = verification["baseline_agg"]

    # leaderboard.json
    json.dump({
        "index": index, "table": table, "objective": objective, "k": k,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_cases_used": verification["n_cases_used"] if verification else len(golden["cases"]),
        "n_cases_total": verification["n_cases_total"] if verification else len(golden["cases"]),
        "scored_on_slice": bool(verification),
        "leaderboard": [{"name": e["name"], "source": e["source"],
                         "objective": e["objective"], "agg": e["agg"],
                         "config": lite(e["candidate"])} for e in ranked],
        "winner": lite(best["candidate"]),
        # full-set numbers when a slice was tuned; else the board's own (already full-set) numbers
        "winner_objective_full": win_obj,
        "winner_agg_full": win_agg,
        "baseline_agg": base_agg,
        "fields_bootstrapped": bootstrapped,
        # boosts the optimizer left pinned at its ceiling, each with whether halving it changes
        # the score. Empty list = none pinned; absent detail = candidate wasn't free to re-score.
        "winner_saturation": best.get("saturation") or [],
    }, open(os.path.join(outdir, "leaderboard.json"), "w"), indent=2)

    # tuned_fields.yaml (the winning config, drop-in for benchmark/<table>/fields.yaml)
    win = best["candidate"]
    scored_on = (f"{verification['n_cases_total']} cases (full set)" if verification
                 else f"{len(golden['cases'])} cases")
    # The file carries BOTH halves of the winner: `fields:` (the boosts) and `query:` (the
    # shape — query type plus tie_breaker/minimum_should_match/phrase_boost). run.py compiles
    # the latter into its `tuned` strategy, so the config that gets applied is the config that
    # was scored. `equivalent_strategy` is only a note about the knob-free strategies.
    strategy = C.equivalent_strategy(win)
    fy = ["# Auto-tuned search config (.claude/skills/tuner/scripts/tuning/tune.py).",
          f"# index {golden.get('index_name')} ({index}); objective {objective}="
          f"{win_obj:.4f} on {scored_on}; winning query_type={win.get('query_type')}"
          + (f"/{win.get('multi_match_type')}" if win.get('multi_match_type') else ""),
          "# Reproduce with:  python3 benchmark/run.py <table> --strategy tuned"]
    if strategy:
        fy.append(f"#   (knob-free shape — the fixed `{strategy}` strategy sends this same "
                  f"query too)")
    fy += ["# DRAFT — review, then copy over benchmark/<table>/fields.yaml and re-run run.py.",
           "",
           "# Which columns to match, and how much each is worth.",
           "fields:"]
    if win.get("fields"):
        fy += [f"  - \"{e}\"" for e in C.to_fields_yaml(win["fields"])]
    else:
        fy.append('  - "*"   # winner searched all fields (no explicit boosts)')
    fy += ["",
           "# The query shape the boosts above were tuned for. Omit this block and the table",
           "# falls back to the fixed strategies, which would score a different query.",
           "query:"]
    qyaml = yaml.dump(C.query_block(win), Dumper=_BlockDumper, sort_keys=False,
                      default_flow_style=False)
    fy += ["  " + line for line in qyaml.rstrip("\n").split("\n")]
    open(os.path.join(outdir, "tuned_fields.yaml"), "w").write("\n".join(fy) + "\n")

    # report.md (experiment log — separate from RESULTS.md)
    md = report_md(objective, golden, board, k, baseline_ev, bootstrapped, verification)
    open(os.path.join(outdir, "report.md"), "w").write(md)

    scored_note = (f"  (full {verification['n_cases_total']}-case set; rounds tuned on "
                   f"{verification['n_cases_used']})" if verification else "")
    print(f"\nwinner: {best['name']}  {objective}={win_obj:.4f}{scored_note}")
    print(f"  {json.dumps(lite(win))}")
    if verification:
        slice_obj = objective_value(best["agg"], objective)
        print(f"  (slice {objective} was {slice_obj:.4f} on {verification['n_cases_used']} cases; "
              f"the {win_obj:.4f} above is the authoritative full-set score)")
    if bootstrapped:
        print("note: no fields.yaml existed for this table — the field list explored was "
              "bootstrapped from the index schema, not read from an existing strategy.")
    _print_saturation(best.get("saturation") or [])
    if base_agg is not None:
        baseline_obj = objective_value(base_agg, objective)
        d = win_obj - baseline_obj
        if best["name"] == "frontend_default" or d <= 1e-6:
            print(f"vs default frontend query: no improvement ({objective}={baseline_obj:.4f}); "
                  f"the default remains the best config found.")
        else:
            print(f"vs default frontend query: {objective} {baseline_obj:.4f} -> {win_obj:.4f} "
                  f"({'+' if d >= 0 else ''}{d:.4f})")
    else:
        print("WARNING: could not compute a vs-default-frontend-query comparison "
              "(frontend_default was never evaluated).")
    print(f"\nwrote {outdir}/" + "{leaderboard.json, tuned_fields.yaml, report.md}")
    print("apply:  cp %s/tuned_fields.yaml benchmark/%s/fields.yaml  &&  "
          "python3 benchmark/run.py %s --strategy tuned --label tuned"
          % (outdir, table, table))


def report_md(objective, golden, board, k, baseline_ev, bootstrapped=False, verification=None):
    obj = objective
    ranked = board.ranked()
    best = ranked[0]

    # Headline + per-case use FULL-set numbers when --max-cases forced a slice (verification);
    # otherwise the board's own numbers already cover every case. The leaderboard table always
    # shows the scores the search actually ran on (slice scores under a slice), clearly labelled.
    if verification:
        win_obj = objective_value(verification["winner_agg"], obj)
        baseline_obj = objective_value(verification["baseline_agg"], obj)
        winner_per_case = verification["winner_per_case"]
        baseline_per_case = verification["baseline_per_case"]
        n_used, n_total = verification["n_cases_used"], verification["n_cases_total"]
    else:
        win_obj = best["objective"]
        baseline_obj = objective_value(baseline_ev["agg"], obj) if baseline_ev else None
        winner_per_case = best["per_case"]
        baseline_per_case = baseline_ev["per_case"] if baseline_ev else {}
        n_used = n_total = len(golden["cases"])
    delta = (win_obj - baseline_obj) if baseline_obj is not None else None

    L = [f"# Search tuning report — {golden.get('index_name')} ({golden['index']})", ""]

    if verification:
        L.append(f"> **Tuned on a {n_used}/{n_total}-case slice** (`--max-cases`). The leaderboard "
                 f"below shows slice scores, but the headline improvement and the per-case table "
                 f"are the winner and `frontend_default` **re-scored live on all {n_total} golden "
                 f"cases** — that full-set number is the one the recommendation stands on.")
        L.append("")

    sat = best.get("saturation") or []
    if sat:
        L.append("> **Saturated boosts.** The optimizer left "
                 + ", ".join(f"`{d['field']}` at {d['boost']}" for d in sat)
                 + " — its ceiling. Ranking depends only on the *ratios* between boosts, so this "
                   "isn't a truncated search (the same ratio is reachable by lowering the other "
                   "fields); it means the field dominates by roughly 10:1, which on a small "
                   "golden set is an overfitting smell. Halving each:")
        L.append("")
        L += [f"| field | boost | halved | {obj} | halved {obj} | verdict |",
              "| --- | --- | --- | --- | --- | --- |"]
        for d in sat:
            verdict = ("no change — the exact value is arbitrary, prefer the smaller one"
                       if d["flat"] else
                       "score drops — the ranking hinges on this one field; brittle, verify on "
                       "more cases")
            L.append(f"| {d['field']} | {d['boost']} | {d['halved']} | {d['score']:.3f} | "
                     f"{d['halved_score']:.3f} | {verdict} |")
        L.append("")

    if bootstrapped:
        L.append("> No `fields.yaml` existed for this table — the field list explored below "
                 "was bootstrapped by profiling the live index (names/categories/free text; "
                 "identifiers excluded), not read from an existing hand-tuned strategy. Treat "
                 "it as a generated starting point.")
        L.append("")

    # Always report the comparison against the default frontend query, front and center —
    # this is the one number a reviewer needs even if they read nothing else. Under a slice it
    # is the full-set re-score, not the slice score.
    full = f" (full {n_total}-case set)" if verification else ""
    if baseline_obj is None:
        L.append("**WARNING: no vs-default-frontend-query comparison available** — "
                 "`frontend_default` was never evaluated in this run.")
    elif best["name"] == "frontend_default" or delta <= 1e-6:
        L.append(f"**No improvement found over the default frontend query**{full} "
                 f"(`frontend_default`, {obj}={baseline_obj:.3f}) — it remains the best config "
                 f"out of everything tried.")
    else:
        L.append(f"**Recommended config improves {obj} by "
                 f"{'+' if delta >= 0 else ''}{delta:.3f} over the default frontend query**{full} "
                 f"(`frontend_default`): {baseline_obj:.3f} → {win_obj:.3f}.")
    L.append("")

    lb_scope = (f"slice of {n_used}/{n_total} cases" if verification
                else f"{n_total} golden cases")
    L += [f"Objective: **{obj}@{k}**.  Leaderboard scored on {lb_scope}.  "
         f"Run {time.strftime('%Y-%m-%d', time.gmtime())}.", "",
         "Query-time tuning only (query type + field selection + boosts). The full winning "
         "config — boosts and query shape — is written to `tuned_fields.yaml`; copy it over "
         "`benchmark/<table>/fields.yaml` and re-run "
         "`benchmark/run.py <table> --strategy tuned` to confirm on the live index.", "",
         "## Leaderboard" + (f" (slice scores, {n_used}/{n_total} cases)" if verification else ""), "",
         f"| config | source | {obj} | MRR | Recall@{k} | Hit@1 | nDCG@{k} |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for e in ranked[:12]:
        a = e["agg"]
        L.append(f"| {e['name']} | {e['source']} | {e['objective']:.3f} | {a['mrr']:.3f} | "
                 f"{a['recall_at_k']:.3f} | {a['hit_at_1']:.3f} | {a['ndcg_at_k']:.3f} |")
    L += ["", "## Winner", "", "```json", json.dumps(lite(best["candidate"]), indent=2), "```", ""]
    if baseline_obj is not None:
        L.append(f"vs the default frontend query (`frontend_default`){full}: {obj} "
                 f"{baseline_obj:.3f} → {win_obj:.3f} "
                 f"({'+' if delta >= 0 else ''}{delta:.3f}).")
        L.append("")
        pc_scope = f"full {n_total}-case set" if verification else "all cases"
        L += [f"## Per-case (winner vs frontend_default — {pc_scope})", "",
              f"| case | query | base {obj} | winner {obj} |", "| --- | --- | --- | --- |"]
        bk = OBJ_CASE_KEY[obj]
        # under a slice, cover every case using verification's full case list; else the golden here
        cases = verification["cases"] if verification else golden["cases"]
        for c in cases:
            bp = baseline_per_case.get(c["id"], {}).get(bk, 0.0)
            wp = winner_per_case.get(c["id"], {}).get(bk, 0.0)
            mark = " ⬆" if wp > bp + 1e-6 else (" ⬇" if wp < bp - 1e-6 else "")
            L.append(f"| {c['id']} | {c['query']} | {bp:.3f} | {wp:.3f}{mark} |")
    return "\n".join(L) + "\n"


# --- `init` / `add-candidates` / `finalize`: agent-driven, no nested API key --------------

def _state_path(outdir):
    return os.path.join(outdir, "state.json")


def _save_state(outdir, state):
    # Write-then-rename so an interrupted write (now that we checkpoint mid-round, a kill is
    # most likely *during* a save) can't truncate/corrupt the recoverable state.json.
    path = _state_path(outdir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _load_state(outdir):
    path = _state_path(outdir)
    if not os.path.exists(path):
        sys.exit(f"no state.json in {outdir} — run `tune.py init <table>` first")
    return json.load(open(path))


def _init_key(table, golden, boosts, objective, k):
    """Fingerprint of everything that would invalidate a partially-completed round 0.

    Scores are only reusable if the table, field list, objective, cutoff, and the exact case
    set they were measured against are all unchanged — change any of them and the persisted
    numbers describe a different experiment. Hashing the queries (not just their count) also
    catches an edited golden.yaml."""
    h = hashlib.sha256()
    h.update(f"{table}|{objective}|{k}".encode())
    h.update("|".join(sorted(boosts)).encode())
    for c in golden["cases"]:
        h.update(f"{c['id']}={c['query']}".encode())
    return h.hexdigest()[:16]


def _load_state_if(outdir, key):
    """A prior state.json for this exact setup, or None. A mismatched or unreadable one is
    ignored rather than fatal — the cost is redoing round 0, not a wrong answer."""
    path = _state_path(outdir)
    if not os.path.exists(path):
        return None
    try:
        prior = json.load(open(path))
    except Exception:
        return None
    return prior if prior.get("init_key") == key else None


def _write_round_context(outdir, profile_brief_dict, gsum, allowed, boosts, board, diagnostics, state):
    """`profile_brief_dict` is the already-briefed, cached profile (see cmd_init) — not a raw
    index_profile.profile() result — so this doesn't re-brief or re-fetch it."""
    rounds_remaining = MAX_ROUNDS - state["round"]
    ctx = {
        "round_completed": state["round"],
        "profile": profile_brief_dict,
        "golden_summary": gsum,
        "allowed_fields": allowed,
        "current_default_boosts": boosts,
        "fields_bootstrapped": state.get("bootstrapped", False),
        "leaderboard": board.summary(),
        "diagnostics": diagnostics,
        "candidate_schema": C.AGENT_CANDIDATE_SCHEMA,
        "stale_rounds": state["stale"],
        "max_rounds": MAX_ROUNDS,
        "max_candidates_per_round": MAX_CANDIDATES_PER_ROUND,
        "rounds_remaining": rounds_remaining,
        "recommendation": (
            "round cap reached — finalize now; add-candidates will refuse another round"
            if rounds_remaining <= 0 else
            "no improvement for 2+ rounds — consider `tune.py finalize` instead of proposing again"
            if state["stale"] >= 2 else
            f"propose another batch of up to {MAX_CANDIDATES_PER_ROUND} candidates, or finalize "
            f"if satisfied ({rounds_remaining} round(s) left before the hard cap)"
        ),
    }
    json.dump(ctx, open(os.path.join(outdir, "round_context.json"), "w"), indent=2)


def cmd_init(args):
    golden_full, boosts, bootstrapped, types = load_table(args.table, args.poll)
    n_total = len(golden_full["cases"])
    # Tune against the (optional) --max-cases slice for speed, but keep the FULL golden in state:
    # the final winner is re-verified on all cases at finalize, never on the slice alone.
    golden = sliced_golden(golden_full, args.max_cases)
    n_used = len(golden["cases"])
    k = args.k or golden.get("k", 10)
    outdir = run_dir(args.table)
    os.makedirs(outdir, exist_ok=True)
    if n_used < n_total:
        print(f"NOTE: tuning on a {n_used}/{n_total}-case slice (--max-cases {args.max_cases}). "
              f"Round scores are slice scores; the winner is re-verified on all {n_total} cases "
              f"at finalize.")

    # Resume a round 0 that was killed or timed out partway: reuse the seed scores already
    # persisted, as long as they were produced for the SAME setup. --fresh forces a clean start.
    key = _init_key(args.table, golden, boosts, args.objective, k)
    resume = None
    if not args.fresh:
        prior = _load_state_if(outdir, key)
        if prior:
            done = len(prior.get("board_entries") or {})
            if prior.get("round", 0) > 0:
                sys.exit(f"{outdir}/state.json is already {prior['round']} round(s) in — `init` "
                         f"would discard that progress. Continue with `add-candidates`, or pass "
                         f"--fresh to start over.")
            print(f"resuming a previous init for this setup ({done} candidate(s) already "
                  f"scored; pass --fresh to start over)")
            resume = prior

    # Persist after every seed so a kill costs at most one seed's worth of live queries.
    partial = {"table": args.table, "init_key": key, "round": 0, "stale": 0}

    def save_partial(bd, base_ev):
        _save_state(outdir, {**partial, "board_entries": bd.entries, "baseline_ev": base_ev})

    board, pm, index, id_field, baseline_ev = _seed_and_probe(
        args.table, golden, boosts, args.objective, k, args.poll, args.workers, outdir,
        bootstrapped, types=types, resume=resume, on_progress=save_partial)
    allowed = sorted(boosts)

    print("\nprofiling index for the proposal step ...")
    # The index profile is immutable across rounds — brief it once here and cache in state so
    # add-candidates can reuse it instead of re-issuing a live 100-doc profiling query each round.
    prof = profile_brief(profile_index(index, 100, poll_s=args.poll))
    gsum = golden_summary(golden, k, n_cases_total=n_total)

    state = {
        "table": args.table, "init_key": key,
        "objective": args.objective, "k": k, "poll": args.poll,
        "workers": args.workers, "index": index, "id_field": id_field,
        "allowed": allowed, "boosts": boosts, "golden": golden_full, "bootstrapped": bootstrapped,
        # cached so add-candidates can apply the same phrase-safety guard without re-querying
        "types": types,
        "max_cases": args.max_cases, "n_cases_total": n_total,
        "board_entries": board.entries, "baseline_ev": baseline_ev,
        "profile": prof, "golden_summary": gsum,
        "round": 0, "best_obj": board.best()["objective"], "stale": 0,
    }
    _save_state(outdir, state)
    diags = _diagnose_vs_winner(pm, golden, board, args.objective)
    _write_round_context(outdir, prof, gsum, allowed, boosts, board, diags, state)

    print(f"\nbaseline best: {board.best()['name']} {args.objective}={board.best()['objective']:.4f}")
    print(f"wrote {outdir}/state.json, round_context.json")
    print("next: read round_context.json, write candidates matching its candidate_schema to "
          "a JSON file ({\"candidates\": [...]}), then:")
    print(f"  python3 tune.py add-candidates {args.table} <candidates.json>")


def cmd_add_candidates(args):
    outdir = run_dir(args.table)
    state = _load_state(outdir)
    if state["round"] >= MAX_ROUNDS:
        sys.exit(f"round cap reached ({MAX_ROUNDS} rounds) for {args.table} — run "
                 f"`tune.py finalize {args.table}` instead of proposing another round.")
    board = Board(state["objective"], entries=state["board_entries"])
    # Rounds tune against the same slice init used (keeps the probe cache key stable); the full
    # golden lives in state["golden"] and is only used for the final winner verification.
    golden = sliced_golden(state["golden"], state.get("max_cases"))
    index, id_field, k = state["index"], state["id_field"], state["k"]
    allowed = state["allowed"]

    pm = probe(index, golden, allowed, id_field, state["poll"],
               cache_path=os.path.join(outdir, "probe.json"), workers=state["workers"])

    def confirm(cand):
        return evaluate(live_ranker(index, cand, id_field, state["poll"]), golden, k, state["workers"])

    def live_score_fn(cand):
        return objective_value(confirm(cand)["agg"], state["objective"])

    raw = json.load(open(args.candidates_file))
    candidates = raw.get("candidates") if isinstance(raw, dict) else raw
    if len(candidates) > MAX_CANDIDATES_PER_ROUND:
        print(f"  note: {len(candidates)} candidates submitted, truncating to the first "
              f"{MAX_CANDIDATES_PER_ROUND} (hard cap per round)")
        candidates = candidates[:MAX_CANDIDATES_PER_ROUND]
    round_num = state["round"] + 1
    n_live = sum(1 for c in candidates if not is_local_rerankable(C.to_candidate(c)))
    print(f"\n=== round {round_num}/{MAX_ROUNDS}: {len(candidates)} proposed candidates ===")
    if n_live:
        print(f"  ({n_live} of {len(candidates)} need live-query optimization — slower; the "
              f"rest tune for free off the probe)")

    # Checkpoint the board after every candidate: each is minutes of live queries, so if this
    # round is killed/times out partway, the completed candidates are already persisted and a
    # re-run of add-candidates continues from them rather than repeating the work.
    def checkpoint(bd):
        state["board_entries"] = bd.entries
        _save_state(outdir, state)

    _process_candidates(candidates, board, golden, pm, state["objective"], k, allowed,
                        round_num, confirm, live_score_fn, on_committed=checkpoint,
                        phrase_unsafe=phrase_unsafe_fields(state.get("types")))

    cur = board.best()["objective"]
    improved = cur > state["best_obj"] + 1e-6
    state["stale"] = 0 if improved else state["stale"] + 1
    state["best_obj"] = max(cur, state["best_obj"])
    state["round"] = round_num
    state["board_entries"] = board.entries
    _save_state(outdir, state)

    # profile + golden_summary were computed once in cmd_init and cached in state — the index
    # profile is immutable across rounds, so reuse them rather than re-issuing a live query.
    diags = _diagnose_vs_winner(pm, golden, board, state["objective"])
    _write_round_context(outdir, state["profile"], state["golden_summary"], allowed,
                         state["boosts"], board, diags, state)

    status = "improved" if improved else f"stale x{state['stale']}"
    print(f"\nround {round_num} best: {board.best()['name']} {state['objective']}={cur:.4f} ({status})")
    if state["stale"] >= 2:
        print("no improvement for 2 rounds — recommend `tune.py finalize` rather than proposing again.")


def cmd_finalize(args):
    outdir = run_dir(args.table)
    state = _load_state(outdir)
    board = Board(state["objective"], entries=state["board_entries"])
    golden_full = state["golden"]   # full set (init stored the unsliced golden)
    # The board holds slice scores when --max-cases was used; re-verify the winner + baseline
    # on the full golden set so the recommendation never rests on a subset.
    verification = verify_full(state["objective"], golden_full, board, state["index"],
                               state["id_field"], state["k"], state["boosts"], state["poll"],
                               state["workers"], state.get("max_cases"))
    # write_outputs uses `golden` only for the index name/id and the leaderboard case-count
    # label; the slice it was scored on is what the leaderboard reflects, so pass the slice.
    golden_slice = sliced_golden(golden_full, state.get("max_cases"))
    write_outputs(state["table"], state["objective"], golden_slice, board,
                  outdir, state["index"], state["id_field"], state["k"],
                  state.get("baseline_ev"), state.get("bootstrapped", False), verification)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)

    def path_args(p):
        """On every subcommand: all three write to <out>/<table>/, and all but `finalize` also
        read the golden set from <bench>/<table>/."""
        p.add_argument("--bench", metavar="DIR", default=None,
                       help="directory holding <table>/{golden,fields}.yaml to READ "
                            "(default: $TUNER_WORK_DIR/benchmark, else this checkout's benchmark/)")
        p.add_argument("--out", metavar="DIR", default=None,
                       help="directory to WRITE run artifacts under, as <DIR>/<table>/ "
                            f"(default: $TUNER_WORK_DIR/{RUNS_DIRNAME}, else "
                            f"./{RUNS_DIRNAME}/ under the directory you run from)")

    p_init = sub.add_parser("init", help="seed+probe+optimize, write round_context.json for an agent to read")
    path_args(p_init)
    p_init.add_argument("table", help="benchmark/<table>/ to tune (reads golden.yaml; bootstraps "
                                      "fields.yaml from the index schema if none exists)")
    p_init.add_argument("--objective", choices=OBJECTIVES, default="ndcg")
    p_init.add_argument("--k", type=int, default=None)
    p_init.add_argument("--poll", type=float, default=0.15)
    p_init.add_argument("--workers", type=int, default=8,
                        help="concurrent index queries (queries are independent async jobs)")
    p_init.add_argument("--max-cases", type=int, default=None,
                        help="use only the first N golden cases (fast smoke test)")
    p_init.add_argument("--fresh", action="store_true",
                        help="ignore any partially-completed round 0 in <out>/<table>/state.json "
                             "and re-score every seed from scratch")
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add-candidates", help="consume agent-authored candidates.json, optimize+confirm live")
    p_add.add_argument("table")
    p_add.add_argument("candidates_file")
    path_args(p_add)
    p_add.set_defaults(func=cmd_add_candidates)

    p_fin = sub.add_parser("finalize", help="write leaderboard.json/tuned_fields.yaml/report.md from current state")
    p_fin.add_argument("table")
    path_args(p_fin)
    p_fin.set_defaults(func=cmd_finalize)

    args = ap.parse_args()
    resolve_paths(args)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show progress even when piped
    except Exception:
        pass
    args.func(args)


if __name__ == "__main__":
    main()
