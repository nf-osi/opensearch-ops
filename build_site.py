#!/usr/bin/env python3
"""Build the NF Search Lab static site into site/ for GitHub Pages.

The site is an interactive browser app (web/) with two faces: a **benchmark results**
dashboard that reports the committed runs in benchmark/<table>/results/, and a **search
lab** that runs live queries (and live scoring) against the public Synapse API
client-side. This script assembles it:

  1. copy the web/ app shell (html/css/js) into the output dir, and
  2. for every benchmark/<table>/golden.yaml, emit site/data/<table>.json — the full
     golden set (queries + per-case provenance), the field/boost config, and the runs
     **site.yaml selects** (per-case detail included), so the dashboard can show rank
     landings, per-case heatmaps and failure lists without re-querying, and
  3. emit site/data/manifest.json — the cross-index portfolio: which nf- SearchIndexes
     exist, which have a golden set, which have a committed search config, plus the
     headline metrics of each table's newest run so the overview renders from one file.

Which runs are published is declared in site.yaml, not inferred from what happens to be
in results/ — so a scratch run can't quietly reach the site, and the build warns when a
selected run is missing, uncommitted (CI builds from a fresh checkout, so it would be
absent there), or present-but-unaccounted-for.

The golden sets, field boosts and results stay single-sourced (run.py reads the same
files); the app never hand-copies them. Dependency: pyyaml (already required by run.py).

Lives at the repo root since it aggregates across all per-table benchmarks.

Usage:
  python3 build_site.py [--benchmark benchmark] [--web web] [--out site] [--config site.yaml]
"""
import argparse, glob, importlib.util, json, os, re, shutil, subprocess, time
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))  # repo root


def _golden_fingerprint():
    """Borrow run.py's dataset and field-config fingerprints instead of re-implementing them.

    Unlike load_field_config below — reimplemented here to keep the two files independent —
    a second hash implementation could not be allowed to drift: the moment the two disagree
    every run looks incomparable to the current golden, which is a false alarm that reads
    exactly like a real one. So there is one definition, and it lives with the writer.
    """
    spec = importlib.util.spec_from_file_location(
        "benchmark_run", os.path.join(HERE, "benchmark", "run.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.golden_fingerprint, mod.field_config_fingerprint


golden_fingerprint, field_config_fingerprint = _golden_fingerprint()

# The strategy every other one is measured against. This is per-table — a table whose
# portal page sets a SearchQueryConfig is controlled against `production_current`, one that
# does not against the platform default — so run.py records the choice in each run JSON
# (`control`, from fields.yaml). This constant is only the fallback for runs written before
# that field existed.
DEFAULT_BASELINE_KEY = "frontend_default"
# Which runs get published, and why — see the file's own comments.
SITE_CONFIG = "site.yaml"
# the run row that measures a portal's own search configuration (mirrors PRODUCTION_KEY in
# web/bench.js) — what a promotion should eventually be recorded `as:`
PRODUCTION_KEY = "production_current"
# Metrics the portfolio overview needs per strategy; the full per-run payload in
# data/<table>.json keeps everything run.py recorded.
HEADLINE_METRICS = ("mrr", "recall_at_k", "hit_at_1", "hit_at_k",
                    "rt_ms_median", "rt_ms_p95", "n_cases")


def fp_str(fp):
    """A run's dataset fingerprint as one readable token: '2026.09.09.1/890cb2799874 (66 cases)'.

    A run written before run.py recorded one renders as 'unfingerprinted'. Those are
    reported as their own state (unverifiable), never compared hash-to-hash: two missing
    fingerprints are not evidence of a shared dataset, and treating them as a mismatch
    would be just as wrong."""
    if not fp:
        return "unfingerprinted"
    return (f"{fp.get('version') or 'unversioned'}/{fp.get('hash')} "
            f"({fp.get('n_cases')} cases)")


def boost_delta(old_fields, new_fields):
    """Human-readable diff between two boosted field lists: '+aiSummary, -race, sex^2->^1'.

    Reported rather than just flagged: "different boost config" sends someone diffing two
    files, while the changed weights are the whole content of the message.
    """
    old, new = dict(map(parse_boost, old_fields)), dict(map(parse_boost, new_fields))
    parts = [f"+{f}" + (f"^{w}" if w != 1 else "") for f, w in new.items() if f not in old]
    parts += [f"-{f}" for f in old if f not in new]
    parts += [f"{f}^{old[f]}->^{w}" for f, w in new.items() if f in old and old[f] != w]
    return ", ".join(parts) or "same fields and weights (block order or `production:` differs)"


def parse_boost(field):
    """'resourceName^5' -> ('resourceName', 5); 'description' -> ('description', 1)."""
    name, _, w = field.partition("^")
    return name, (int(w) if w else 1)


def load_field_config(table_dir):
    """The table's match list with per-field boosts, any tuned query shape, the
    `production:` block and the control (benchmark/<table>/fields.yaml) — same contract as
    run.py: fields fall back to ['*'] (all fields, no boosts), `query:` is absent for tables
    whose strategies are all fixed shapes, `production:` is absent for tables whose portal
    page ships no SearchQueryConfig, and the control defaults to the platform default."""
    path = os.path.join(table_dir, "fields.yaml")
    if not os.path.exists(path):
        return ["*"], None, None, DEFAULT_BASELINE_KEY
    cfg = yaml.safe_load(open(path)) or {}
    return ((cfg.get("fields") or ["*"]), cfg.get("query"),
            cfg.get("production"), cfg.get("control") or DEFAULT_BASELINE_KEY)


def rank_bucket(rank):
    """Where the first correct result landed, as the four bands the dashboard plots.
    Mirrored in web/charts.js (rankBucket) — keep the two in step."""
    if not rank:
        return "miss"
    if rank == 1:
        return "top"
    if rank <= 3:
        return "near"
    return "deep"


def bucket_counts(per_case_for_strategy, k):
    """{top, near, deep, miss} over one strategy's per-case results. A first-relevant
    rank beyond k counts as a miss: the case's correct answer exists but is off the
    page the metrics are scored on."""
    out = {"top": 0, "near": 0, "deep": 0, "miss": 0}
    for sc in (per_case_for_strategy or {}).values():
        rank = sc.get("first_rel_rank")
        out[rank_bucket(rank if rank and rank <= k else None)] += 1
    return out


def short_path(path):
    """Repo-relative when the path is inside the repo, as given otherwise — so an error
    about a file elsewhere doesn't print a wall of `../`."""
    rel = os.path.relpath(path, HERE)
    return path if rel.startswith("..") else rel


def load_site_config(path):
    """Read site.yaml — the explicit list of which runs get published. A missing file is
    an error rather than a silent fallback to "publish everything in results/": which
    runs the site reports is exactly the thing that should not drift by accident."""
    if not os.path.exists(path):
        raise SystemExit(f"missing {short_path(path)} — it selects which benchmark runs "
                         f"are published; see the copy in git history")
    cfg = yaml.safe_load(open(path)) or {}
    return {
        "headline": cfg.get("headline") or "latest",
        "extra_runs": cfg.get("extra_runs") or {},
        "excluded": cfg.get("excluded") or {},
        "constant": cfg.get("constant") or {},
        "promoted": cfg.get("promoted") or {},
    }


def git_tracked(paths_under):
    """The set of git-tracked files under `paths_under`, or None if git can't answer.

    Used to catch the failure mode that only bites after publishing: a results file that
    exists locally but was never committed. CI builds from a fresh checkout, so that run
    is simply absent there — the site loses a run and nothing says why."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", paths_under],
                             cwd=HERE, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {os.path.join(HERE, rel) for rel in out.stdout.split("\0") if rel}


def selected_labels(table, cfg):
    """The labels to publish for one table, in run-switcher order: the run of record
    first, then whatever site.yaml adds for this table."""
    extra = cfg["extra_runs"].get(table) or []
    return [cfg["headline"], *[lb for lb in extra if lb != cfg["headline"]]]


def apply_constants(runs, table, cfg, results_dir, report):
    """Pin a strategy's row to a fixed index state, as declared by site.yaml `constant:`.

    A run holds one index state, but the platform default is only the platform default on
    an UNCUSTOMIZED index — so where the index carries a bound search config, that row has
    to come from a run taken without one. This rewrites the in-memory payload only; the
    files in results/ are never touched, so `run.py` cannot quietly undo the substitution
    and the source run stays independently readable.

    Distinct from `control:` in a table's fields.yaml, which names the strategy the rest of
    a run is read against; this is about which run a row is measured in.

    Every substitution is recorded on the run as `constants[strategy] = {from, run_at}` so
    the dashboard can mark the row, and reported at build time so it can never be silent."""
    spec = (cfg["constant"] or {}).get(table) or {}
    if not spec:
        return
    cache = {}
    for run in runs:
        for strategy, source_label in spec.items():
            if run["label"] == source_label:
                continue                      # a run is already constant against itself
            if strategy not in run["strategies"]:
                continue                      # this run did not score it; nothing to replace
            if source_label not in cache:
                src_path = os.path.join(results_dir, f"{source_label}.json")
                try:
                    cache[source_label] = json.load(open(src_path))
                except (ValueError, OSError) as e:
                    cache[source_label] = None
                    report["unreadable"].append(f"{short_path(src_path)} (constant source): {e}")
            src = cache[source_label]
            if not src or strategy not in src.get("strategies", {}):
                report["missing"].append(
                    f"{table}: constant source {source_label!r} has no {strategy!r} row")
                continue
            # A splice is a claim that the two runs differ only in index state. If they
            # scored different cases it is also a dataset swap, and the pinned row is then
            # measured on a different denominator than the rows beside it.
            src_fp, run_fp = src.get("golden") or {}, run.get("golden") or {}
            if src_fp.get("hash") and run_fp.get("hash") \
                    and src_fp["hash"] != run_fp["hash"]:
                report["golden_drift"].append(
                    f"{table}/{run['label']}: {strategy!r} pinned to {source_label!r}, which "
                    f"scored a different golden ({fp_str(src_fp)} vs {fp_str(run_fp)}) — the "
                    f"pinned row is not comparable to the rest of the run")
            run["strategies"][strategy] = src["strategies"][strategy]
            if strategy in src.get("per_case", {}):
                run["per_case"][strategy] = src["per_case"][strategy]
            run.setdefault("constants", {})[strategy] = {
                "from": source_label, "run_at": src.get("run_at"),
            }
            report["constants"].append(
                f"{table}/{run['label']}: {strategy} taken from {source_label}")


def read_promotion(table, cfg, runs, report):
    """The site.yaml `promoted:` record for one table, validated against the headline run.

    An entry naming a strategy the run does not score would put a label on a row that is
    not there, so it is dropped and reported rather than published. Dates come back from
    YAML as `datetime.date`; the payload carries strings."""
    spec = (cfg["promoted"] or {}).get(table) or {}
    if not spec:
        return None
    key = spec.get("as")
    run = headline_run(runs, cfg["headline"]) if runs else None
    if not key:
        report["unaccounted"].append(f"{table}: `promoted:` has no `as:` — dropped")
        return None
    if not run or key not in (run.get("strategies") or {}):
        report["unaccounted"].append(
            f"{table}: `promoted: as: {key}` is not a strategy in the "
            f"{cfg['headline']!r} run — dropped")
        return None
    out = {"as": key, "at": str(spec["at"]) if spec.get("at") else None,
           "note": spec.get("note")}
    ranked = sorted(run["strategies"].items(), key=lambda kv: kv[1].get("mrr") or 0, reverse=True)
    # whether the deployed row is also the best row is what the dashboard leads with, so
    # state it here rather than leaving every view to re-derive it
    out["is_best"] = bool(ranked) and ranked[0][0] == key
    # A promotion may be recorded the moment it goes live, naming the experiment arm that
    # measures the newly deployed shape; the run's own production_current row then measures
    # the configuration that was replaced. That is a deliberate, temporary state — so
    # compare the two dates and say when it has outlived itself.
    has_prod = PRODUCTION_KEY in (run.get("strategies") or {})
    superseded = key != PRODUCTION_KEY and has_prod
    run_day = (run.get("run_at") or "")[:10]
    if out["at"] and run_day:
        if key == PRODUCTION_KEY and run_day < out["at"]:
            report["promotion_drift"].append(
                f"{table}: promoted `at: {out['at']}` is after the {run['label']!r} run "
                f"({run_day}) — that run's {PRODUCTION_KEY} row measures the configuration "
                f"the promotion replaced. Re-score it, or point `as:` at the arm that "
                f"measures the deployed shape.")
        elif superseded and run_day > out["at"]:
            report["promotion_drift"].append(
                f"{table}: the {run['label']!r} run ({run_day}) postdates promoted "
                f"`at: {out['at']}` — re-transcribe `production:` in "
                f"benchmark/{table}/fields.yaml and set `as: {PRODUCTION_KEY}`, so one row "
                f"measures what is deployed.")
    report["promoted"].append(
        f"{table}/{key}{' (best arm)' if out['is_best'] else ''}"
        f"{' · ' + out['at'] if out['at'] else ''}"
        f"{' · supersedes ' + PRODUCTION_KEY if superseded else ''}")
    return out


def load_runs(table_dir, table, cfg, tracked, report, current_fp, current_fcfp):
    """The runs site.yaml selects for this table, in run-switcher order.

    The whole run is carried through — per_case included — because the dashboard's rank
    landings, per-case heatmap and failure list are all per-case views. Runs are a handful
    of strategies x tens of cases, so the payload stays in the low hundreds of KB.

    Everything the selection implies is reported rather than assumed: a selected run that
    is missing or uncommitted, a run sitting in results/ that nothing accounts for, and a
    run whose dataset fingerprint does not match `current_fp` (the table's golden as it is
    on disk now) or the other published runs — a comparison across two datasets is the one
    failure that still renders as a clean-looking chart."""
    results_dir = os.path.join(table_dir, "results")
    runs, published = [], []
    for label in selected_labels(table, cfg):
        path = os.path.join(results_dir, f"{label}.json")
        rel = os.path.relpath(path, HERE)
        if not os.path.exists(path):
            # only worth flagging for the headline: an extra run named for a table that
            # hasn't been scored yet is a plan, not a problem
            if label == cfg["headline"]:
                report["missing"].append(f"{rel} (no scored run yet — run benchmark/run.py {table})")
            else:
                report["missing"].append(rel)
            continue
        if tracked is not None and path not in tracked:
            report["uncommitted"].append(rel)
        try:
            out = json.load(open(path))
        except (ValueError, OSError) as e:
            report["unreadable"].append(f"{rel}: {e}")
            continue
        runs.append({
            "label": out.get("label") or label,
            "run_at": out.get("run_at"),
            "k": out.get("k"),
            # which strategy this run was controlled against, as recorded by run.py from
            # the table's fields.yaml; absent on runs predating per-table controls
            "control": out.get("control"),
            # SearchConfiguration bound while this run was scored (None = uncustomized);
            # recorded by run.py. Used below to catch a platform-default row measured
            # against a customized index.
            "search_config_id": out.get("search_config_id"),
            # the dataset this run scored — golden `version:` + content hash, recorded by
            # run.py. Absent on runs predating it, which is why every check below treats a
            # missing fingerprint as unverifiable rather than as matching.
            "golden": out.get("golden"),
            # the boosts/production block this run scored with, recorded by run.py. The
            # payload's table-level `fields` is fields.yaml as it is NOW; this is what the
            # numbers actually came from, and the two are not always the same file.
            "field_config": out.get("field_config"),
            "strategies": out.get("strategies", {}),
            "per_case": out.get("per_case", {}),
        })
        published.append(label)

    # Published runs sit side by side in the dashboard's run switcher, which only reads as
    # a comparison if they scored the same cases. Checked before apply_constants so the
    # fingerprints compared are the ones the runs were written with.
    fingerprinted = [r for r in runs if (r.get("golden") or {}).get("hash")]
    if len({r["golden"]["hash"] for r in fingerprinted}) > 1:
        report["golden_drift"].append(
            f"{table}: published runs did not all score the same golden — "
            + "; ".join(f"{r['label']}={fp_str(r.get('golden'))}" for r in fingerprinted))
    # …and a run scored against an older golden than the one on disk today is a stale
    # number, however recent its run_at looks.
    for run in fingerprinted:
        if run["golden"]["hash"] != current_fp["hash"]:
            report["golden_drift"].append(
                f"{table}/{run['label']}: scored {fp_str(run['golden'])}, but "
                f"{table}/golden.yaml is now {fp_str(current_fp)} — re-score it or exclude it")
    # Runs from before run.py recorded a fingerprint: nothing to compare, which is itself
    # worth saying once — their comparability rests on the assumption that the golden has
    # not moved since, and that is exactly what cannot be checked.
    # A boost change between runs is the experiment, not a fault — but the payload's
    # table-level `fields` is today's fields.yaml, so a run scored under different weights
    # is captioned with weights that did not produce it. Say which, and with what delta;
    # each run now carries `field_config` for the dashboard to caption from.
    for run in runs:
        fc = run.get("field_config")
        if fc and fc.get("hash") != current_fcfp["hash"]:
            report["boosts"].append(
                f"{table}/{run['label']}: scored under boosts {fc['hash']}, not today's "
                f"{current_fcfp['hash']} — "
                f"{boost_delta(fc.get('fields') or [], current_fcfp['fields'])}")

    unverifiable = [r["label"] for r in runs if not (r.get("golden") or {}).get("hash")]
    if unverifiable:
        report["golden_drift"].append(
            f"{table}: no dataset fingerprint on {', '.join(unverifiable)} — re-score under "
            f"the same label(s) to make comparability checkable")

    apply_constants(runs, table, cfg, results_dir, report)

    # A published run that scored the platform default against a bound index is reporting
    # the default QUERY on a CUSTOMIZED index. Pinning the row to an unbound run fixes it;
    # not noticing does not. Warn unless a `constant:` entry already covers it.
    for run in runs:
        if (run.get("search_config_id")
                and "frontend_default" in run["strategies"]
                and "frontend_default" not in (run.get("constants") or {})):
            report["unbound_default"].append(
                f"{table}/{run['label']}: frontend_default scored with config "
                f"{run['search_config_id']} bound — not a true platform default. "
                f"Score it unbound and add a `constant:` entry (see site.yaml).")

    # account for every other file in results/: excluded on purpose, or unaccounted for
    excluded = cfg["excluded"].get(table) or {}
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        label = os.path.splitext(os.path.basename(path))[0]
        if label in published:
            continue
        if label in excluded:
            report["excluded"].append(f"{table}/{label}")
        else:
            report["unaccounted"].append(
                f"{os.path.relpath(path, HERE)} — add it to extra_runs or excluded in {SITE_CONFIG}")
    return runs, published


def build_table_data(golden_path, table, cfg, tracked, report):
    """Assemble one table's data/<table>.json payload from its golden + fields + results."""
    table_dir = os.path.dirname(os.path.abspath(golden_path))
    golden = yaml.safe_load(open(golden_path))
    current_fp = golden_fingerprint(golden)
    fields, query, production, control = load_field_config(table_dir)
    # fields.yaml as it stands NOW. Runs record their own copy, so the two can be compared
    # rather than the current one being assumed to describe every published number.
    current_fcfp = field_config_fingerprint(
        fields, production, os.path.exists(os.path.join(table_dir, "fields.yaml")))
    # Carry the SME-facing provenance fields through (source, expected_pool, reviewer,
    # …) — they are what the dashboard's failure list and provenance panel report on.
    cases = [{"id": c["id"], "query": c["query"], "type": c.get("type"),
              "relevant": c.get("relevant", []), "notes": c.get("notes"),
              "source": c.get("source"), "source_url": c.get("source_url"),
              "expected_pool": c.get("expected_pool"),
              "entity_types": c.get("entity_types"),
              # the two recall-gap flags: `legacy` is a hand-recorded historical fact about
              # the pre-OpenSearch portal search (the dashboard's legacy-gap section reads
              # it), `current` is the golden's own note about the live index
              "recall_gap_legacy_search": bool(c.get("recall_gap_legacy_search")),
              "recall_gap_current": bool(c.get("recall_gap_current")),
              "last_reviewed": str(c["last_reviewed"]) if c.get("last_reviewed") else None,
              "reviewer": c.get("reviewer")}
             for c in golden.get("cases", [])]
    runs = load_runs(table_dir, table, cfg, tracked, report, current_fp, current_fcfp)[0]
    return {
        "index": golden["index"],
        "index_name": golden.get("index_name"),
        "version": golden.get("version"),
        # the current golden's content hash, so the dashboard can tell a run scored against
        # today's cases from one scored against an earlier edit of them
        "golden_hash": current_fp["hash"],
        "k": golden.get("k", 10),
        "id_field": golden.get("id_field", "resourceId"),
        "fields": fields,
        "query": query,
        "production": production,
        "control": control,
        "cases": cases,
        "runs": runs,
        # what is actually deployed, when site.yaml records a promotion (see `promoted:`)
        "promoted": read_promotion(table, cfg, runs, report),
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }


def headline_run(runs, headline):
    """Which run the dashboard opens on: site.yaml's `headline` label (run.py's default,
    i.e. the most recent full invocation). It wins even when an extra published run
    carries a newer timestamp and would otherwise headline the index with a partial
    strategy set. Every published run stays available in the run switcher."""
    for run in runs:
        if run["label"] == headline:
            return run
    return runs[0]


def table_summary(table, data, headline):
    """The portfolio row for one table: golden-set shape plus the newest run's headline
    metrics for the production baseline and the best-MRR strategy. Enough for the
    overview to rank and plot every index without fetching each data/<table>.json."""
    types = {}
    for c in data["cases"]:
        types[c["type"] or "unspecified"] = types.get(c["type"] or "unspecified", 0) + 1
    summary = {
        "table": table, "index": data["index"], "index_name": data["index_name"],
        "k": data["k"], "n_cases": len(data["cases"]), "types": types,
        "n_fields": len(data["fields"]), "tuned_query": bool(data["query"]),
        "promoted": data.get("promoted"),
        "runs": [{"label": r["label"], "run_at": r["run_at"],
                  "n_strategies": len(r["strategies"])} for r in data["runs"]],
        "latest": None,
    }
    if not data["runs"]:
        return summary
    run = headline_run(data["runs"], headline)
    summary["headline_label"] = run["label"]
    k = run["k"] or data["k"]
    scored = {key: {m: agg.get(m) for m in HEADLINE_METRICS}
              for key, agg in run["strategies"].items()}
    ranked = sorted(scored.items(), key=lambda kv: kv[1].get("mrr") or 0, reverse=True)
    best_key = ranked[0][0] if ranked else None
    baseline_key = run.get("control") or DEFAULT_BASELINE_KEY
    summary["latest"] = {
        "label": run["label"], "run_at": run["run_at"], "k": k,
        "strategies": scored,
        "baseline_key": baseline_key if baseline_key in scored else None,
        "best_key": best_key,
        # cases the run covered vs cases in the golden set today: goldens grow between
        # runs, and a stale run should say so rather than quietly under-report.
        "n_cases_run": max((len(pc) for pc in run["per_case"].values()), default=0),
        # the rank-landing mix for the two strategies the overview draws
        "buckets": {key: bucket_counts(run["per_case"].get(key), k)
                    for key in {baseline_key, best_key} & set(run["per_case"])},
    }
    return summary


# Row shape in the README's "NF index objects" table:
#   | `syn75081630` | `nf-datasets` | `SELECT * FROM syn50913342` |
README_ROW = re.compile(r"^\|\s*`(syn\d+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|", re.M)


def load_index_registry(readme_path):
    """The full nf- SearchIndex inventory, parsed from the README table that already
    documents it, so the dashboard's coverage panel has a denominator ("5 of 13 indexes
    have a golden set") without a second copy of the list. Returns [] if the table is
    missing or renamed — the overview then just shows the benchmarked indexes."""
    try:
        text = open(readme_path).read()
    except OSError:
        return []
    rows = []
    for index_id, name, sql in README_ROW.findall(text):
        if not name.startswith("nf-"):
            continue
        src = re.search(r"FROM\s+(syn\d+)", sql)
        rows.append({"index": index_id, "index_name": name,
                     "source_table": src.group(1) if src else None})
    return rows


def load_configured(config_dir):
    """Index names with a committed search config in config/ (nf_tools_search_config.json
    -> nf-tools). Presence of the file means the config is version-controlled here, not
    that it is currently bound in production — `python3 config/config.py check` is what
    reports binding state."""
    names = set()
    for path in glob.glob(os.path.join(config_dir, "*_search_config.json")):
        names.add(os.path.basename(path).replace("_search_config.json", "").replace("_", "-"))
    return names


def print_report(report, cfg):
    """State what the run selection did, including what it left out. A build that drops a
    run silently reads as "everything is published" when it isn't."""
    if report["excluded"]:
        print(f"  excluded by {SITE_CONFIG}: {', '.join(report['excluded'])}")
    for line in report["constants"]:
        print(f"  held constant: {line}")
    for line in report["promoted"]:
        print(f"  in production: {line}")
    for line in report["promotion_drift"]:
        print(f"  WARNING: {line}")
    for rel in report["missing"]:
        print(f"  note: selected run not on disk — {rel}")
    for rel in report["uncommitted"]:
        print(f"  WARNING: {rel} is not committed — the published site will not have it")
    for line in report["unreadable"]:
        print(f"  WARNING: unreadable run {line}")
    for line in report["unaccounted"]:
        print(f"  WARNING: unpublished run not accounted for: {line}")
    for line in report["unbound_default"]:
        print(f"  WARNING: {line}")
    for line in report["golden_drift"]:
        print(f"  WARNING: {line}")
    for line in report["boosts"]:
        print(f"  note: {line}")


def build(benchmark_dir, web_dir, out_dir, config_path):
    # 1. copy the app shell
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(web_dir, out_dir)

    # 2. generate per-table data files + the portfolio manifest the dashboard opens on
    cfg = load_site_config(config_path)
    tracked = git_tracked(os.path.relpath(benchmark_dir, HERE))
    report = {k: [] for k in ("missing", "uncommitted", "unreadable", "excluded",
                              "unaccounted", "constants", "promoted", "promotion_drift",
                              "unbound_default",
                              "golden_drift", "boosts")}
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    generated_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    tables = []
    for golden_path in sorted(glob.glob(os.path.join(benchmark_dir, "*", "golden.yaml"))):
        table = os.path.basename(os.path.dirname(golden_path))
        try:
            data = build_table_data(golden_path, table, cfg, tracked, report)
        except (KeyError, ValueError) as e:
            print(f"  skip {table}: {e}")
            continue
        json.dump(data, open(os.path.join(data_dir, f"{table}.json"), "w"), indent=2)
        tables.append(table_summary(table, data, cfg["headline"]))
        labels = ", ".join(r["label"] for r in data["runs"]) or "no published run"
        print(f"  data/{table}.json  ({len(data['cases'])} cases · {labels})")

    registry = load_index_registry(os.path.join(HERE, "README.md"))
    configured = load_configured(os.path.join(HERE, "config"))
    benchmarked = {t["index_name"]: t["table"] for t in tables}
    for row in registry:
        row["table"] = benchmarked.get(row["index_name"])
        row["configured"] = row["index_name"] in configured
    manifest = {
        "generated_at": generated_at,
        "default_baseline_key": DEFAULT_BASELINE_KEY,
        # what the run selection resolved to, so the published payload records the policy
        # it was built under rather than leaving it implicit in site.yaml's history
        "runs_published": {t["table"]: [r["label"] for r in t["runs"]] for t in tables},
        "headline_label": cfg["headline"],
        "coverage": {
            "n_indexes": len(registry) or len(tables),
            "n_benchmarked": len(tables),
            "n_configured": len([r for r in registry if r["configured"]]) or len(configured),
            "n_cases": sum(t["n_cases"] for t in tables),
        },
        "registry": registry,
        "tables": tables,
    }
    json.dump(manifest, open(os.path.join(data_dir, "manifest.json"), "w"), indent=2)
    print(f"  data/manifest.json  ({len(tables)} benchmarked of {manifest['coverage']['n_indexes']} "
          f"nf- indexes, {manifest['coverage']['n_cases']} cases)")
    print_report(report, cfg)
    return out_dir, [t["table"] for t in tables]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=os.path.join(HERE, "benchmark"))
    ap.add_argument("--web", default=os.path.join(HERE, "web"))
    ap.add_argument("--out", default=os.path.join(HERE, "site"))
    ap.add_argument("--config", default=os.path.join(HERE, SITE_CONFIG),
                    help=f"which runs to publish (default: {SITE_CONFIG})")
    args = ap.parse_args()
    out, tables = build(args.benchmark, args.web, args.out, args.config)
    print(f"wrote {out}/ — tables: {', '.join(tables) or '(none)'}")


if __name__ == "__main__":
    main()
