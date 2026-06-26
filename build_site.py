#!/usr/bin/env python3
"""Render benchmark results/*.json into a static HTML page for GitHub Pages.

Stub: reads every <table>/results/<label>.json and emits site/index.html with
one sortable-ish table per run. Intentionally dependency-free (stdlib only) so
the CI job doesn't need anything beyond what run.py already installs.

Lives at the repo root (not under benchmark/) since it aggregates results across
all per-table benchmarks.

Usage:
  python3 build_site.py [--results benchmark] [--out site]
"""
import argparse, glob, html, json, os, time

HERE = os.path.dirname(os.path.abspath(__file__))  # repo root

COLS = [("mrr", "MRR"), ("recall_at_k", "Recall@k"), ("hit_at_1", "Hit@1"),
        ("hit_at_k", "Hit@k"), ("rt_ms_median", "rt_ms_med"), ("rt_ms_p95", "rt_ms_p95")]


def run_table(out):
    k = out.get("k")
    rows = sorted(out["strategies"].items(), key=lambda kv: kv[1]["mrr"], reverse=True)
    head = "".join(f"<th>{html.escape(lbl.replace('@k', f'@{k}'))}</th>" for _, lbl in COLS)
    body = []
    for name, a in rows:
        cells = [f"<td class=name>{html.escape(name)}</td>"]
        for key, _ in COLS:
            v = a.get(key, 0.0)
            cells.append(f"<td>{v:.0f}</td>" if key.startswith("rt_ms") else f"<td>{v:.3f}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    label = html.escape(str(out.get("label", "run")))
    ran = f" · {out['run_at']}" if out.get("run_at") else ""
    meta = html.escape(f"{out.get('index_name')} ({out.get('index')}) · k={k} · "
                       f"{out['strategies'][rows[0][0]]['n_cases']} cases{ran}")
    return (f"<section><h2>{label}</h2><p class=meta>{meta}</p>"
            f"<table><thead><tr><th>strategy</th>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></section>")


def build(results_dir, out_dir, generated_at):
    # per-table layout: benchmark/<table>/results/*.json; also accept a direct
    # results dir (…/results/*.json) for back-compat.
    paths = sorted(glob.glob(os.path.join(results_dir, "*", "results", "*.json"))
                   or glob.glob(os.path.join(results_dir, "*.json")))
    sections = []
    for p in paths:
        try:
            sections.append(run_table(json.load(open(p))))
        except (KeyError, ValueError, IndexError):
            continue
    if not sections:
        sections.append(
            "<section class=wip><h2>🚧 Under construction</h2>"
            "<p>No benchmark results yet. Once <code>benchmark/run.py</code> produces a "
            "<code>results/*.json</code> file, this page will show query-strategy scores "
            "(MRR / Recall / Hit) for each run.</p></section>")

    page = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>nf-tools search benchmark</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 880px;
         padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ margin-bottom: .2rem; }}
  .sub {{ color: #666; margin-top: 0; }}
  .meta {{ color: #666; font-size: .9em; margin: .2rem 0 .6rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
  th, td {{ border: 1px solid #ddd; padding: .4rem .6rem; text-align: right; }}
  th:first-child, td.name {{ text-align: left; }}
  thead th {{ background: #0E8C7F; color: #fff; }}
  tbody tr:nth-child(even) {{ background: #f6f8f8; }}
  .wip {{ background: #fff8e6; border: 1px solid #f0d98a; border-radius: 8px;
          padding: .2rem 1.2rem; }}
  footer {{ color: #888; font-size: .85em; margin-top: 3rem; }}
</style></head><body>
<h1>nf-tools search benchmark</h1>
<p class=sub>Query-strategy quality (MRR / Recall / Hit) against the golden relevance set.
See the repo for methodology &amp; strategy definitions.</p>
{''.join(sections)}
<footer>Generated {html.escape(generated_at)} · higher MRR/Recall/Hit is better, lower rt_ms is better.</footer>
</body></html>"""

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(page)
    return os.path.join(out_dir, "index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(HERE, "benchmark"))
    ap.add_argument("--out", default=os.path.join(HERE, "site"))
    args = ap.parse_args()
    path = build(args.results, args.out, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
