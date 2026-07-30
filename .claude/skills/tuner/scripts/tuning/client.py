"""The tuner's own Synapse SearchIndex client — what this harness needs to talk to
repo-prod, and nothing else.

The deployed repo-prod API takes a *raw OpenSearch query DSL* object in `searchQuery`
(e.g. {"query": {...}, "size": N}) — not the structured queryType/queryFields model in the
published rest-docs OpenAPI, which is ahead of what is deployed. Auth is not required: these
indexes are public and every call here works anonymously. Pass token=... only for a
non-public index.

Deliberately standalone. This skill bundles no code from any other skill and walks no
directory tree looking for a shared client, so the same files run identically from a repo
checkout and from a read-only skill bundle. The repo's `query.py` (used by benchmark/run.py
and config/config.py) and `goldie`'s `synapse_client.py` cover the same endpoints
for their own consumers; each is self-contained for the same reason. Keep the request shapes
here in sync with those if the API ever changes.
"""
import json
import re
import time
import urllib.error
import urllib.request

BASE = "https://repo-prod.prod.sagebase.org/repo/v1"
# The master index collections project — every SearchIndex is a child of it.
INDEX_COLLECTIONS = "syn74909065"


def _call(ep, method="GET", body=None, token=None):
    """One repo-prod call. Returns (status_code, parsed_body) — HTTP errors come back as a
    code, not an exception, because the callers below treat 4xx as data ("no such entity")."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/{ep}", data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def search(search_index_id, search_query,
           response_parts=("HITS", "TOTAL_HITS", "SELECT_COLUMNS"),
           timeout_s=30, token=None, poll_s=0.5):
    """Run an async SearchIndex query and return the SearchQueryResults dict.

    search_query: raw OpenSearch DSL, e.g. {"query": {"match": {"description": {"query":
    "schwann"}}}, "size": 10}.
    poll_s: interval between job-status polls (lower it when timing queries).
    """
    payload = {
        "concreteType": "org.sagebionetworks.repo.model.search.table.SearchIndexQuery",
        "searchIndexId": search_index_id,
        "searchQuery": search_query,
    }
    if response_parts:
        payload["responseParts"] = list(response_parts)
    code, body = _call("search/query/async/start", "POST", payload, token)
    if code not in (200, 201):
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


# --- column types: which fields a free-text query may touch, and how -----------------------
#
# A SearchIndex column's Synapse type determines its OpenSearch mapping, and getting this wrong
# is not a ranking problem — the index rejects the query outright with a 500. Verified live
# against nf-studies (syn75081633):
#
#   STRING / STRING_LIST / LARGETEXT -> analyzed `text`. Safe for every query type.
#   ENTITYID / ENTITYID_LIST         -> `keyword`. Term queries fine; phrase/phrase_prefix
#                                       fails: "Can only use phrase prefix queries on text
#                                       fields - not on [...] which is of type [keyword]".
#   DATE / INTEGER / DOUBLE / …      -> numeric. ANY text query fails:
#                                       "number_format_exception: For input string: "mpnst"".
#
# So a field list must exclude the numeric/temporal types entirely, and phrase-shaped queries
# must additionally exclude the keyword ones.
TEXT_TYPES = frozenset({"STRING", "STRING_LIST", "LARGETEXT"})
KEYWORD_TYPES = frozenset({"ENTITYID", "ENTITYID_LIST", "USERID", "USERID_LIST",
                           "FILEHANDLEID", "SUBMISSIONID", "EVALUATIONID",
                           "LINK", "LINK_LIST"})


def is_text_searchable(ctype):
    """Can a free-text query match this column at all? Unknown types are allowed through —
    better to let the index be the judge of a type this list hasn't seen than to silently
    drop a searchable field."""
    return ctype not in ("DATE", "DATE_LIST", "INTEGER", "INTEGER_LIST", "DOUBLE",
                         "BOOLEAN", "BOOLEAN_LIST", "JSON")


def is_phrase_safe(ctype):
    """Can this column take a phrase / phrase_prefix query? Only analyzed text can."""
    return ctype not in KEYWORD_TYPES and is_text_searchable(ctype)


def column_types(search_index_id, token=None, poll_s=0.2):
    """{column_name: columnType} for an index, from one cheap match_all.

    The index reports its own schema in `selectColumns`, which is authoritative — far better
    than inferring types from sampled string values (an epoch-millis DATE looks exactly like a
    high-variety identifier)."""
    res = search(search_index_id, {"query": {"match_all": {}}, "size": 1},
                 response_parts=["SELECT_COLUMNS"], token=token, poll_s=poll_s)
    return {c["name"]: c.get("columnType") for c in (res.get("selectColumns") or [])}


def hit_id(hit, id_field):
    """The stable identifier for a hit, per the golden's id_field.

    `id_field` is the column whose value the golden's `relevant` ids come from (e.g.
    resourceId for nf-tools; it varies by portal/table). Use "rowId" to key on the index's own
    per-row id, which is always present and needs no column.
    """
    if id_field == "rowId":
        return hit.get("rowId")
    return hit_dict(hit).get(id_field)


# --- resolving what to tune (SKILL.md's index-name / source-table-id inputs) ---------------

def list_search_indexes(parent=INDEX_COLLECTIONS, token=None):
    """List every SearchIndex child of `parent`, paginated. [{"id": "synNNN", "name": ...}].

    `includeTypes: ["searchindex"]` is required — SearchIndex entities are not returned by a
    default entity/children listing."""
    out = []
    next_token = None
    while True:
        body = {"parentId": parent, "includeTypes": ["searchindex"]}
        if next_token:
            body["nextPageToken"] = next_token
        code, page = _call("entity/children", "POST", body, token)
        if code >= 400:
            raise RuntimeError(f"listing children of {parent} failed ({code}): {page}")
        out.extend({"id": c["id"], "name": c["name"]} for c in page.get("page", []))
        next_token = page.get("nextPageToken")
        if not next_token:
            return out


def resolve_index_name(name, parent=INDEX_COLLECTIONS, token=None):
    """Find a SearchIndex by exact name. Returns (index_id, index_name), or (None, None) if
    no SearchIndex with that name exists — reported plainly rather than guessing a near match,
    because tuning the wrong index is worse than declining."""
    for c in list_search_indexes(parent, token):
        if c["name"] == name:
            return c["id"], c["name"]
    return None, None


def resolve_table_to_index(table_id, parent=INDEX_COLLECTIONS, token=None):
    """Find the SearchIndex whose definingSQL sources from `table_id`. Returns
    (index_id, index_name), or (None, None) if none does — most likely because no index has
    been built for that table yet, which means there is nothing to tune."""
    for c in list_search_indexes(parent, token):
        code, ent = _call(f"entity/{c['id']}", token=token)
        if code >= 400:
            continue
        sql = ent.get("definingSQL") or ent.get("definingSql") or ""
        m = re.search(r"\bfrom\s+(syn\d+)", sql, re.I)
        if m and m.group(1) == table_id:
            return c["id"], c["name"]
    return None, None
