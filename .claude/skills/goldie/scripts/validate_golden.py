#!/usr/bin/env python3
"""Validate a benchmark golden.yaml — structure AND live resolvability.

Two classes of check:

  ERRORS (exit 1 — the file is broken or the benchmark would silently misscore):
    - YAML doesn't parse / no `id_field` / no `cases`
    - duplicate case ids
    - a case missing `query`, missing/empty `relevant`, or with duplicate relevant ids
    - id_field resolves to nothing in the index (wrong id_field)
    - *** a `relevant` id that does NOT exist in the index ***  <- the key gate
        The runner matches `relevant` against the index's id_field values (run.py). An id
        that isn't in the index scores ZERO and is indistinguishable from a real recall
        gap — a stale/typo'd id silently corrupts the benchmark. This must be caught here.

  WARNINGS (printed, exit still 0):
    - duplicate `query` strings across cases
    - a `type` other than topical / known-item
    - a lopsided split (almost no topical cases — the skill wants MOSTLY topical)

The id-in-index gate pages through the whole index (match_all) once and checks membership,
so it needs network. Use --no-index for a fast, offline structural-only pass.

Invoke from the repo root:
  python3 .claude/skills/goldie/scripts/validate_golden.py benchmark/<table>/golden.yaml [--no-index] [--token ...]
"""
import argparse, collections, os, sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # synapse_client is a sibling — this skill is self-contained
from synapse_client import search, hit_dict  # noqa: E402

VALID_TYPES = {"topical", "known-item"}


def index_id_values(index_id, id_field, token=None, page=100, cap=10000):
    """Collect every id_field value present in the index by paging match_all.

    Returns (set_of_values, total_hits). cap guards the OpenSearch max_result_window;
    goldens only exist for tables that passed the size gate (≤ ~10k), so one full page-through
    is bounded. id_field == "rowId" keys on the index's own per-row id."""
    got, frm, total = set(), 0, 0
    while frm < cap:
        res = search(index_id, {"query": {"match_all": {}}, "size": page, "from": frm},
                     response_parts=["HITS", "TOTAL_HITS"], token=token, poll_s=0.15)
        hits = res.get("hits", [])
        total = res.get("totalHits") or total
        for h in hits:
            v = h.get("rowId") if id_field == "rowId" else hit_dict(h).get(id_field)
            if v is not None:
                got.add(v)
        frm += page
        if len(hits) < page or (total and frm >= total):
            break
    return got, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("golden", help="path to benchmark/<table>/golden.yaml")
    ap.add_argument("--no-index", action="store_true",
                    help="skip the live id-in-index gate (offline, structure only)")
    ap.add_argument("--token", default=None, help="Synapse token (only if the index isn't public)")
    args = ap.parse_args()

    errors, warnings = [], []

    try:
        g = yaml.safe_load(open(args.golden))
    except Exception as e:
        print(f"ERROR: {args.golden} does not parse: {e}")
        sys.exit(1)
    if not isinstance(g, dict):
        print("ERROR: top level is not a mapping")
        sys.exit(1)

    id_field = g.get("id_field")
    if not id_field:
        errors.append("no `id_field` set (the runner needs it to match hits)")
    version = g.get("version")
    if version is None:
        warnings.append("no `version` set (default: today's date as YYYY.MM.DD)")
    cases = g.get("cases")
    if not cases:
        print("ERROR: no `cases` in file")
        sys.exit(1)

    # --- structural checks ---
    ids = [c.get("id") for c in cases]
    dup_ids = [i for i, n in collections.Counter(ids).items() if i and n > 1]
    if dup_ids:
        errors.append(f"duplicate case ids: {dup_ids}")
    if None in ids:
        errors.append("some case(s) have no `id`")

    queries = [c.get("query") for c in cases]
    dup_q = [q for q, n in collections.Counter(queries).items() if q and n > 1]
    if dup_q:
        warnings.append(f"duplicate query strings across cases: {dup_q}")

    all_relevant = set()
    for c in cases:
        cid = c.get("id", "<no-id>")
        if not c.get("query"):
            errors.append(f"[{cid}] missing `query`")
        rel = c.get("relevant") or []
        if not rel:
            errors.append(f"[{cid}] empty/missing `relevant`")
        dup_rel = [r for r, n in collections.Counter(rel).items() if n > 1]
        if dup_rel:
            errors.append(f"[{cid}] duplicate ids in `relevant`: {dup_rel}")
        all_relevant.update(rel)
        t = c.get("type")
        if t not in VALID_TYPES:
            warnings.append(f"[{cid}] type {t!r} not in {sorted(VALID_TYPES)}")

    topical = sum(1 for c in cases if c.get("type") == "topical")
    if cases and topical < len(cases) * 0.4:
        warnings.append(f"only {topical}/{len(cases)} cases are topical — the skill wants MOSTLY topical")

    # --- live id-in-index gate (the point of #2) ---
    index_checked = False
    if not args.no_index and id_field and all_relevant:
        index_id = g.get("index")
        if not index_id:
            # No index built yet (or not wired up). Generation is source-derived, so this is
            # not an error — the gate (like recall_gap_current) just defers until the index exists.
            warnings.append("no `index` id in file — skipping the id-in-index gate "
                            "(fine if the SearchIndex isn't built yet; re-run this once it is)")
        else:
            print(f"resolving {len(all_relevant)} relevant ids against index {index_id} (id_field={id_field})…")
            try:
                present, total = index_id_values(index_id, id_field, args.token)
            except Exception as e:
                present, total = None, 0
                warnings.append(f"could not query index {index_id} ({e}) — skipping the id-in-index gate "
                                "(is it built yet? re-run once it is)")
            if present is None:
                pass  # index unreachable — already warned; don't fail generation on a missing index
            elif total and not present:
                errors.append(f"id_field {id_field!r} matched NOTHING across {total} indexed rows — wrong id_field?")
            elif not total:
                warnings.append(f"index {index_id} returned 0 rows — skipping the id-in-index gate "
                                "(index empty or still building; re-run once populated)")
            else:
                index_checked = True
                missing = collections.defaultdict(list)
                for c in cases:
                    for r in (c.get("relevant") or []):
                        if r not in present:
                            missing[c.get("id", "<no-id>")].append(r)
                if missing:
                    n = sum(len(v) for v in missing.values())
                    errors.append(f"{n} `relevant` id(s) NOT found in the index "
                                  f"(these score 0 and fake a recall gap):")
                    for cid, rs in missing.items():
                        for r in rs:
                            errors.append(f"    [{cid}] {r}")

    # --- report ---
    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        print()
        for e in errors:
            print(f"ERROR: {e}")
        print(f"\nFAILED — {len(errors)} error(s), {len(warnings)} warning(s).")
        sys.exit(1)

    if index_checked:
        gate = "all resolve in index"
    elif args.no_index:
        gate = "index gate skipped (--no-index)"
    else:
        gate = "index gate skipped — re-run once the index is built"
    print(f"\nOK — id_field: {id_field}  version: {version or '(unset)'}  cases: {len(cases)} "
          f"({topical} topical / {len(cases) - topical} known-item)  "
          f"relevant ids: {len(all_relevant)}  ({gate})"
          f"  warnings: {len(warnings)}")


if __name__ == "__main__":
    main()
