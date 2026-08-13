#!/usr/bin/env python3
"""Verify nf- SearchIndex health and (optionally) repair unhealthy ones.

Context: OpenSearch indexing occasionally produces a failed or incomplete
index (stack issues, transient errors during the weekly automated rebuild)
that the platform's own fix does not catch 100% of the time. This script is
the fallback safety net: it checks that each index is (1) queryable and (2)
holding roughly the number of documents its source table actually has, so we
find out from a scheduled job rather than from someone noticing bad results
in the UI.

Checks per index:
  - queryable: a `match_all` query completes without error.
  - count sane: totalHits is compared against `SELECT COUNT(*)` on the
    source table parsed out of the index's own `definingSQL` (both run
    anonymously, same as the index's own anonymous indexing process, so the
    two counts should track each other closely). A 0-vs-nonzero mismatch is
    always a failure; otherwise a --tolerance relative drift is allowed
    (indexing lags the source table slightly under normal operation).

Auth: reads (querying, entity/table lookups) are anonymous. --repair needs a
token with modify scope on the SearchIndex entities — reads it from
$NF_SERVICE_TOKEN, else the `NF_SERVICE_TOKEN=` line in ~/.bashrc, else
--token (same convention as config/config.py, which --repair also reuses to
trigger the rebuild).

Usage:
  python3 monitor/check_index.py                       # check all nf- indices
  python3 monitor/check_index.py syn75081636            # check just nf-tools
  python3 monitor/check_index.py --tolerance 0.2        # allow 20% drift
  python3 monitor/check_index.py --repair               # ...and rebuild any unhealthy index
  python3 monitor/check_index.py --out report.json      # also write a JSON report

Exit code is non-zero if any checked index is unhealthy (for CI alerting).
"""
import argparse, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "config"))
from query import _call, BASE, STAGING_BASE, search  # noqa: E402
from config import get_token, rebuild, SEARCH_INDEX_COLLECTION  # noqa: E402

NAME_PREFIX = "nf-"
DEFAULT_TOLERANCE = 0.1  # allowed relative drift between index totalHits and source row count

FROM_RE = re.compile(r"\bFROM\s+(syn\d+)\b(.*)$", re.IGNORECASE | re.DOTALL)
ORDER_OR_LIMIT_RE = re.compile(r"\b(ORDER\s+BY|LIMIT)\b.*$", re.IGNORECASE | re.DOTALL)


def list_nf_indices(base):
    """Return [{'id', 'name'}] for every SearchIndex under the shared collection
    project whose name starts with NAME_PREFIX."""
    results = []
    next_page_token = None
    while True:
        payload = {"parentId": SEARCH_INDEX_COLLECTION, "includeTypes": ["searchindex"]}
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        code, body = _call("entity/children", "POST", payload, base=base)
        if code >= 400:
            sys.exit(f"could not list SearchIndex children ({code}): {json.dumps(body)[:500]}")
        results.extend(
            {"id": r["id"], "name": r["name"]}
            for r in body.get("page", [])
            if r.get("name", "").startswith(NAME_PREFIX)
        )
        next_page_token = body.get("nextPageToken")
        if not next_page_token:
            break
    return results


def parse_source(defining_sql):
    """Derive (source_id, count_sql) from a SearchIndex's definingSQL, e.g.
    'SELECT * FROM syn123 ORDER BY x ASC' -> ('syn123', 'SELECT COUNT(*) FROM syn123').
    ORDER BY/LIMIT are dropped (COUNT queries reject them); a WHERE clause, if
    present, is kept since it affects which rows are counted. Returns (None, None)
    if the SQL doesn't match the expected shape.
    """
    m = FROM_RE.search(defining_sql or "")
    if not m:
        return None, None
    source_id, rest = m.group(1), m.group(2)
    rest = ORDER_OR_LIMIT_RE.sub("", rest).strip()
    return source_id, f"SELECT COUNT(*) FROM {source_id} {rest}".strip()


def table_row_count(source_id, count_sql, base, token=None, timeout_s=30, poll_s=1.0):
    """Run count_sql (a `SELECT COUNT(*) ...` query) against source_id and return the int result."""
    payload = {
        "concreteType": "org.sagebionetworks.repo.model.table.QueryBundleRequest",
        "entityId": source_id,
        "query": {"sql": count_sql},
    }
    code, body = _call(f"entity/{source_id}/table/query/async/start", "POST", payload, token, base=base)
    if code not in (200, 201):
        raise RuntimeError(f"count query start failed ({code}): {body}")
    tok = body["token"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, body = _call(f"entity/{source_id}/table/query/async/get/{tok}", token=token, base=base)
        if code == 202 or (isinstance(body, dict) and body.get("jobState") == "PROCESSING"):
            time.sleep(poll_s)
            continue
        if code >= 400:
            raise RuntimeError(f"count query failed ({code}): {body.get('reason', body)}")
        rows = body["queryResult"]["queryResults"]["rows"]
        return int(rows[0]["values"][0]) if rows else 0
    raise TimeoutError(f"count query did not complete within {timeout_s}s")


def check_index(idx, base, tolerance):
    """Return a health report dict for one index: status is one of
    OK / EMPTY / MISMATCH / UNQUERYABLE / ERROR."""
    report = {"id": idx["id"], "name": idx["name"], "status": "OK", "issues": []}

    code, ent = _call(f"entity/{idx['id']}", base=base)
    if code >= 400:
        report["status"] = "ERROR"
        report["issues"].append(f"could not read entity ({code}): {ent}")
        return report

    try:
        res = search(idx["id"], {"query": {"match_all": {}}, "size": 0}, timeout_s=30)
        report["totalHits"] = res.get("totalHits", 0)
    except Exception as e:
        report["status"] = "UNQUERYABLE"
        report["issues"].append(f"query failed: {e}")
        return report

    source_id, count_sql = parse_source(ent.get("definingSQL"))
    if not source_id:
        report["issues"].append("could not parse source table from definingSQL; skipped count check")
        return report

    try:
        expected = table_row_count(source_id, count_sql, base)
        report["expected"] = expected
    except Exception as e:
        report["issues"].append(f"could not verify expected count ({source_id}): {e}")
        return report

    total_hits = report["totalHits"]
    if expected > 0 and total_hits == 0:
        report["status"] = "EMPTY"
        report["issues"].append(f"index has 0 documents but source ({source_id}) has {expected} rows")
    elif expected > 0:
        drift = abs(total_hits - expected) / expected
        report["drift"] = round(drift, 4)
        if drift > tolerance:
            report["status"] = "MISMATCH"
            report["issues"].append(
                f"totalHits={total_hits} vs source rows={expected} "
                f"(drift {drift:.1%} > tolerance {tolerance:.0%})"
            )
    return report


def resolve_targets(index_args, base):
    if not index_args:
        indices = list_nf_indices(base)
        if not indices:
            sys.exit("no nf- SearchIndex objects found")
        return indices
    indices = []
    for sid in index_args:
        code, ent = _call(f"entity/{sid}", base=base)
        if code >= 400:
            sys.exit(f"could not read entity {sid} ({code}): {ent}")
        indices.append({"id": sid, "name": ent.get("name", sid)})
    return indices


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("indices", nargs="*", help="SearchIndex synId(s) to check (default: all nf- indices)")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                     help=f"allowed relative drift between totalHits and source row count (default {DEFAULT_TOLERANCE})")
    ap.add_argument("--repair", action="store_true",
                     help="trigger a rebuild (entity touch) for any unhealthy index found; needs a modify-scope token")
    ap.add_argument("--token")
    ap.add_argument("--staging", action="store_true",
                     help=f"hit the staging repo API ({STAGING_BASE}) instead of prod")
    ap.add_argument("--out", help="also write the JSON report to this path")
    args = ap.parse_args()

    base = STAGING_BASE if args.staging else BASE
    targets = resolve_targets(args.indices, base)

    print(f"Checking {len(targets)} index(es) [{base}]  tolerance={args.tolerance:.0%}\n")
    reports = []
    for idx in sorted(targets, key=lambda i: i["name"]):
        r = check_index(idx, base, args.tolerance)
        reports.append(r)
        line = f"  [{r['status']:^11}] {r['name']} ({r['id']})"
        if "totalHits" in r:
            line += f"  totalHits={r['totalHits']}"
        if "expected" in r:
            line += f"  expected~{r['expected']}"
        print(line)
        for issue in r["issues"]:
            print(f"      - {issue}")

    unhealthy = [r for r in reports if r["status"] != "OK"]

    if unhealthy and args.repair:
        token = get_token(args.token)
        print(f"\nRepair: triggering rebuild for {len(unhealthy)} unhealthy index(es)")
        for r in unhealthy:
            try:
                rebuild(r["id"], token, dry=False, base=base)
                r["repair_triggered"] = True
            except SystemExit as e:
                print(f"  could not repair {r['name']}: {e}")
                r["repair_triggered"] = False

    if args.out:
        Path(args.out).write_text(json.dumps(reports, indent=2))

    print()
    if unhealthy:
        sys.exit("UNHEALTHY: " + ", ".join(f"{r['name']} ({r['status']})" for r in unhealthy))
    print("All indices healthy.")


if __name__ == "__main__":
    main()
