#!/usr/bin/env python3
"""Minimal Synapse repo-prod client — the primitives goldie's other scripts import as a
sibling (profile_index.py, profile_table.py, validate_golden.py):

  _call(ep, ...)          -> low-level authenticated HTTP against the repo-prod API
  search(index, dsl, ...) -> run a raw OpenSearch DSL query on a SearchIndex (async poll)
  hit_dict(hit)           -> flatten a SearchHit's fields into {column: value}

The deployed repo-prod SearchIndex API accepts a *raw OpenSearch query DSL* object in
`searchQuery` (e.g. {"query": {...}, "size": N, "from": N}). Auth is not required for public
indexes; pass token=... for a non-public one.

CLI (read raw records for feel):
  python3 .claude/skills/goldie/scripts/synapse_client.py '{"query":{"match_all":{}},"size":20}' <INDEX_ID>
"""
import json, sys, time, urllib.request, urllib.error

BASE = "https://repo-prod.prod.sagebase.org/repo/v1"


def _call(ep, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/{ep}", data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def search(search_index_id, search_query,
           response_parts=("HITS", "TOTAL_HITS", "SELECT_COLUMNS"),
           timeout_s=30, token=None, poll_s=0.5):
    """Run an async SearchIndex query and return the SearchQueryResults dict.

    search_query: raw OpenSearch DSL, e.g. {"query": {"match": {"description": {"query": "schwann"}}}, "size": 10}
    token: optional; not needed for public indexes.
    poll_s: interval between job-status polls (lower it for latency benchmarking).
    """
    payload = {
        "concreteType": "org.sagebionetworks.repo.model.search.table.SearchIndexQuery",
        "searchIndexId": search_index_id,
        "searchQuery": search_query,
    }
    if response_parts:
        payload["responseParts"] = list(response_parts)
    code, body = _call("search/query/async/start", "POST", payload, token)
    if code != 201 and code != 200:
        raise RuntimeError(f"start failed ({code}): {body}")
    tok = body["token"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, body = _call(f"search/query/async/get/{tok}", token=token)
        if code == 202 or (isinstance(body, dict) and body.get("jobState") == "PROCESSING"):
            time.sleep(poll_s)
            continue
        if code >= 400:
            raise RuntimeError(f"query failed ({code}): {body.get('reason', body)}")
        return body
    raise TimeoutError(f"query did not complete within {timeout_s}s")


def hit_dict(hit):
    """Flatten a SearchHit's fields into a {column: value} dict."""
    return {f["name"]: f.get("value") for f in hit.get("fields", [])}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run a raw OpenSearch DSL query against a SearchIndex.")
    ap.add_argument("query", help='OpenSearch DSL as JSON, e.g. \'{"query":{"match_all":{}},"size":20}\'')
    ap.add_argument("index", help="SearchIndex id (synNNN)")
    ap.add_argument("--token", default=None, help="Synapse token (only if the index isn't public)")
    args = ap.parse_args()
    r = search(args.index, json.loads(args.query), token=args.token)
    print(f"totalHits={r.get('totalHits')}  returned={len(r.get('hits', []))}")
    for h in r.get("hits", []):
        # generic dump (schema-agnostic): rowId + flattened fields, one JSON object per line
        row = {"rowId": h.get("rowId"), **hit_dict(h)}
        print(json.dumps(row, ensure_ascii=False))
