#!/usr/bin/env python3
"""Query a Synapse SearchIndex (OpenSearch) and return results.

The deployed repo-prod API accepts a *raw OpenSearch query DSL* object in
`searchQuery` (e.g. {"query": {...}, "size": N, "from": N, "sort": [...]}).
This differs from the structured queryType/queryFields model in the public
rest-docs OpenAPI, which is ahead of what is deployed. Use raw DSL here.

Auth: not required — these indexes are public and queries work anonymously.
Pass token=... to search() only if you need to hit a non-public index.

Usage:
    from query import search, NF_TOOLS
    res = search(NF_TOOLS, {"query": {"match_all": {}}, "size": 5})
    print(res["totalHits"])
"""
import json, sys, time, urllib.request, urllib.error

BASE = "https://repo-prod.prod.sagebase.org/repo/v1"
NF_TOOLS = "syn75081636"  # nf-tools SearchIndex

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
    q = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"query": {"match_all": {}}, "size": 3}
    idx = sys.argv[2] if len(sys.argv) > 2 else NF_TOOLS
    r = search(idx, q)
    print(f"totalHits={r.get('totalHits')}  returned={len(r.get('hits', []))}")
    for h in r.get("hits", []):
        d = hit_dict(h)
        print(f"  [{h.get('score')}] {d.get('resourceName')} ({d.get('resourceType')})")
