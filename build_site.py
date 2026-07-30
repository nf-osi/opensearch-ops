#!/usr/bin/env python3
"""Build the NF Search Lab static site into site/ for GitHub Pages.

The site itself is an interactive browser app (web/) — it runs live searches and the
benchmark against the public Synapse API client-side. This script just assembles it:

  1. copy the web/ app shell (html/css/js) into the output dir, and
  2. for every benchmark/<table>/golden.yaml, emit site/data/<table>.json — the golden
     cases + field-boost config the app needs, plus an optional precomputed baseline
     scoreboard (from results/<label>.json) so the scoreboard has a fast default.

The golden set and field boosts thus stay single-sourced in YAML (run.py reads the same
files); the app never hand-copies them. Dependency: pyyaml (already required by run.py).

Lives at the repo root since it aggregates across all per-table benchmarks.

Usage:
  python3 build_site.py [--benchmark benchmark] [--web web] [--out site]
"""
import argparse, glob, json, os, shutil, time
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))  # repo root


def parse_boost(field):
    """'resourceName^5' -> ('resourceName', 5); 'description' -> ('description', 1)."""
    name, _, w = field.partition("^")
    return name, (int(w) if w else 1)


def load_field_config(table_dir):
    """(fields, query_spec) from benchmark/<table>/fields.yaml — same contract as run.py's
    load_field_config. `fields` is the match list with per-field boosts, falling back to ['*']
    (all fields, no boosts). `query_spec` is the optional `query:` block (the tuned query
    shape); the site ships it so the browser lab can offer the same `tuned` recipe run.py does,
    instead of applying tuned boosts to a differently-shaped query."""
    path = os.path.join(table_dir, "fields.yaml")
    if not os.path.exists(path):
        return ["*"], None
    cfg = yaml.safe_load(open(path)) or {}
    return (cfg.get("fields") or ["*"]), (cfg.get("query") or None)


def find_precomputed(table_dir):
    """Load results/latest.json to embed as the default scoreboard. Returns a slim
    {label, run_at, k, strategies} dict (no per_case — keeps the data file small; the app
    only offers per-case drill-in for live runs) or None if no latest.json exists yet.

    Only latest.json is used (not baseline.json or any other results/*.json) — the
    embedded scoreboard should always reflect the most recent run.py invocation, not a
    frozen historical snapshot."""
    path = os.path.join(table_dir, "results", "latest.json")
    if not os.path.exists(path):
        return None
    try:
        out = json.load(open(path))
    except (ValueError, OSError):
        return None
    return {"label": out.get("label", "latest"), "run_at": out.get("run_at"),
            "k": out.get("k"), "strategies": out.get("strategies", {})}


def build_table_data(golden_path):
    """Assemble one table's data/<table>.json payload from its golden + fields + results."""
    table_dir = os.path.dirname(os.path.abspath(golden_path))
    golden = yaml.safe_load(open(golden_path))
    cases = [{"id": c["id"], "query": c["query"], "type": c.get("type"),
              "relevant": c.get("relevant", []), "notes": c.get("notes")}
             for c in golden.get("cases", [])]
    fields, query_spec = load_field_config(table_dir)
    return {
        "index": golden["index"],
        "index_name": golden.get("index_name"),
        "k": golden.get("k", 10),
        "id_field": golden.get("id_field", "resourceId"),
        "fields": fields,
        # omitted entirely for untuned tables, so app.js's `data.query || null` clears any
        # previously-registered recipe rather than carrying it across a table switch
        **({"query": query_spec} if query_spec else {}),
        "cases": cases,
        "precomputed": find_precomputed(table_dir),
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }


def build(benchmark_dir, web_dir, out_dir):
    # 1. copy the app shell
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(web_dir, out_dir)

    # 2. generate per-table data files + a manifest the app's index picker reads
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    generated_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    manifest = {"generated_at": generated_at, "tables": []}
    for golden_path in sorted(glob.glob(os.path.join(benchmark_dir, "*", "golden.yaml"))):
        table = os.path.basename(os.path.dirname(golden_path))
        try:
            data = build_table_data(golden_path)
        except (KeyError, ValueError) as e:
            print(f"  skip {table}: {e}")
            continue
        json.dump(data, open(os.path.join(data_dir, f"{table}.json"), "w"), indent=2)
        manifest["tables"].append({"table": table, "index": data["index"],
                                   "index_name": data["index_name"], "n_cases": len(data["cases"])})
        print(f"  data/{table}.json  ({len(data['cases'])} cases, "
              f"{'precomputed' if data['precomputed'] else 'no precomputed'})")
    json.dump(manifest, open(os.path.join(data_dir, "manifest.json"), "w"), indent=2)
    return out_dir, [t["table"] for t in manifest["tables"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=os.path.join(HERE, "benchmark"))
    ap.add_argument("--web", default=os.path.join(HERE, "web"))
    ap.add_argument("--out", default=os.path.join(HERE, "site"))
    args = ap.parse_args()
    out, tables = build(args.benchmark, args.web, args.out)
    print(f"wrote {out}/ — tables: {', '.join(tables) or '(none)'}")


if __name__ == "__main__":
    main()
