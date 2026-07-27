#!/usr/bin/env python3
"""Query the SOURCE table behind a SearchIndex — the ground-truth oracle for goldens.

The generate-goldens methodology derives every relevant set from the *source* table's
metadata (the `definingSQL` table), NOT from the search index — deriving relevance by
searching the index is circular (search can't surface what it misses). This helper tools
that oracle so the agent doesn't hand-reconstruct the async Table Query loop each run:

  resolve(index_id)          -> the definingSQL source-table id (+ the SQL)
  list_search_indexes()      -> every SearchIndex in the master index collections project
                                (syn74909065 by default) — [{id, name}, ...]
  resolve_index_name(name)   -> find a SearchIndex by exact name among the above; (id, name)
                                or (None, None) if no such index exists
  resolve_table_to_index(id) -> find the SearchIndex whose definingSQL sources from table
                                `id`; (index_id, index_name) or (None, None) if none does
                                (i.e. no index has been built for that table yet)
  tquery(sql, src)           -> run a Synapse Table Query (async poll) and return the bundle
  rows(bundle)               -> unpack a tquery() bundle into a plain list of value-lists —
                                use this for any ad-hoc query; the bundle's own shape is
                                nested (queryResult.queryResults.rows[].values), not flat
  columns(src)               -> the source table's column models (name / type / facet)
  distributions(src, cols)   -> real GROUP BY value counts for categorical columns —
                                the true topical-query SEEDS and expected_pool numbers

Unlike the index `match_all` sample (order-biased, ≤100 rows — see SKILL.md), these counts
are ground truth: full-table GROUP BYs straight from the source.

Takes a **source table id** by default (queried directly, no extra lookup) — pass
`--index-id` when you only have the SearchIndex id and need it resolved to its source table
first. (For profiling the SearchIndex itself instead of its source table, see
`profile_index.py`.)

Anonymous for public tables; pass --token / token=... for a non-public source table.

Invoke from the repo root:
  python3 sciops/agents/goldie/profile_table.py <SRC_TABLE_ID> [--col NAME ...] [--top N] [--token ...]
  python3 sciops/agents/goldie/profile_table.py <INDEX_ID> --index-id [--col NAME ...] ...
"""
import argparse, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # for the vendored synapse_client (keeps the skill self-contained)
from synapse_client import _call  # noqa: E402


# Concrete types that are themselves queryable source tables (no SearchIndex indirection).
_SOURCE_TYPES = ("TableEntity", "EntityView", "Dataset", "DatasetCollection",
                 "VirtualTable", "MaterializedView", "SubmissionView")


def resolve(entity_id, token=None):
    """Resolve an entity id to a queryable source-table id. Returns (src_id, sql|None).

    Accepts either a SearchIndex (→ its definingSQL source table) OR a source table itself
    (Table/View/Dataset/…). The latter matters when the user points at a table whose
    SearchIndex hasn't been built yet: golden generation is source-derived, so it proceeds
    normally — only the index-dependent steps (recall_gap_current, the id-in-index gate)
    defer until the index exists. sql is None when the id is already a source table."""
    code, ent = _call(f"entity/{entity_id}", token=token)
    if code >= 400:
        raise RuntimeError(f"GET entity/{entity_id} failed ({code}): {ent}")
    ct = ent.get("concreteType", "")
    sql = ent.get("definingSQL") or ent.get("definingSql")

    # A SearchIndex is NOT directly table-queryable — resolve it through definingSQL to the
    # source table. (A MaterializedView/EntityView also has a definingSQL but IS queryable, so
    # we must NOT drill those: querying the index's own source view gets the flattened,
    # joined columns; drilling into that view's definingSQL would drop them.)
    if "SearchIndex" in ct:
        if not sql:
            for v in ent.values():  # be defensive if the schema varies
                if isinstance(v, str) and re.search(r"\bfrom\s+syn\d+", v, re.I):
                    sql = v
                    break
        m = re.search(r"\bfrom\s+(syn\d+)", sql or "", re.I)
        if not m:
            raise RuntimeError(f"SearchIndex {entity_id} definingSQL has no source table: {sql!r}")
        return m.group(1), sql

    # Anything itself queryable (table/view/dataset — index not built yet, or user pointed
    # straight at the source) is queried as-is.
    if ent.get("columnIds") or any(t in ct for t in _SOURCE_TYPES):
        return entity_id, None

    raise RuntimeError(f"entity {entity_id} is neither a SearchIndex nor a "
                       f"queryable table (concreteType={ct!r})")


def list_search_indexes(parent="syn74909065", token=None):
    """List every SearchIndex child of `parent` (the master index collections project),
    paginated. Returns [{"id": "synNNN", "name": "nf-tools"}, ...]."""
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


def resolve_index_name(name, parent="syn74909065", token=None):
    """Find a SearchIndex by exact name among `parent`'s children (the master index
    collections project). Returns (index_id, index_name), or (None, None) if no SearchIndex
    with that name exists there — the caller's job to decide what "no index" means, this
    just reports it plainly rather than guessing a close match."""
    for c in list_search_indexes(parent, token):
        if c["name"] == name:
            return c["id"], c["name"]
    return None, None


def resolve_table_to_index(table_id, parent="syn74909065", token=None):
    """Find the SearchIndex (among `parent`'s children) whose definingSQL sources from
    `table_id` — the reverse of resolve(). Returns (index_id, index_name), or (None, None)
    if no SearchIndex references this table (most likely: none has been built for it yet)."""
    for c in list_search_indexes(parent, token):
        code, ent = _call(f"entity/{c['id']}", token=token)
        if code >= 400:
            continue
        sql = ent.get("definingSQL") or ent.get("definingSql") or ""
        m = re.search(r"\bfrom\s+(syn\d+)", sql, re.I)
        if m and m.group(1) == table_id:
            return c["id"], c["name"]
    return None, None


def tquery(sql, src, token=None, limit=300, part_mask=1, timeout_s=60, poll_s=0.5):
    """Run a Synapse Table Query against `src` (async start + poll) and return the bundle.

    part_mask bits: 1=query results, 4=select columns, 16=column models, 2=count.
    """
    body = {"concreteType": "org.sagebionetworks.repo.model.table.QueryBundleRequest",
            "entityId": src, "query": {"sql": sql, "limit": limit}, "partMask": part_mask}
    code, b = _call(f"entity/{src}/table/query/async/start", "POST", body, token)
    if code not in (200, 201):
        raise RuntimeError(f"table query start failed ({code}): {b}")
    tok = b["token"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, b = _call(f"entity/{src}/table/query/async/get/{tok}", token=token)
        if code == 202 or (isinstance(b, dict) and b.get("jobState") == "PROCESSING"):
            time.sleep(poll_s)
            continue
        if code >= 400:
            raise RuntimeError(f"table query failed ({code}): {b.get('reason', b)}")
        return b
    raise TimeoutError(f"table query did not complete within {timeout_s}s")


def rows(bundle):
    """Unpack a tquery() bundle into a plain list of value-lists — the bundle's own shape is
    nested (queryResult.queryResults.rows[].values), not the flat `bundle["rows"]` you might
    guess at. Use this for any ad-hoc query beyond what columns()/distributions() cover."""
    rs = (bundle.get("queryResult") or {}).get("queryResults") or {}
    return [r.get("values", []) for r in rs.get("rows", [])]


def columns(src, token=None):
    """Return the source table's column models: [{name, columnType, facetType?}, ...]."""
    b = tquery(f"SELECT * FROM {src}", src, token=token, limit=1, part_mask=1 | 4 | 16)
    cm = b.get("columnModels")
    if cm:
        return cm
    # Fall back to selectColumns if the deployment omits columnModels.
    return (b.get("selectColumns") or [])


def distributions(src, cols, token=None, top=25):
    """Real GROUP BY value counts for each column in `cols` (column-model dicts).

    STRING_LIST (multi-value) columns are UNNESTed so each member is counted separately.
    Returns {name: {"type", "truncated", "dist": [(value, count), ...]}} (or {"error"})."""
    out = {}
    for c in cols:
        name = c["name"]
        ctype = c.get("columnType", "") or ""
        expr = f'UNNEST("{name}")' if ctype.endswith("_LIST") else f'"{name}"'
        sql = (f'SELECT {expr}, COUNT(*) FROM {src} '
               f'GROUP BY {expr} ORDER BY COUNT(*) DESC LIMIT {top}')
        try:
            vals = rows(tquery(sql, src, token=token, limit=top))
            out[name] = {"type": ctype, "truncated": len(vals) >= top,
                         "dist": [(r[0], int(r[1])) for r in vals if r]}
        except Exception as e:  # keep going; one bad column shouldn't sink the profile
            out[name] = {"type": ctype, "error": str(e)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("id", help="source table id (synNNN) by default — pass --index-id if "
                               "this is a SearchIndex id instead")
    ap.add_argument("--index-id", action="store_true",
                    help="treat `id` as a SearchIndex id, not a source table id — resolve "
                         "it to its source table first")
    ap.add_argument("--col", action="append", default=[],
                    help="column(s) to distribute (repeatable); default = enumeration + list columns")
    ap.add_argument("--top", type=int, default=25, help="max distinct values per column")
    ap.add_argument("--token", default=None, help="Synapse token (only if the source table isn't public)")
    args = ap.parse_args()

    if args.index_id:
        src, sql = resolve(args.id, args.token)
        print(f"index {args.id}  ->  source table {src}")
        if sql:
            print(f"definingSQL: {sql}")
    else:
        src, sql = args.id, None
        print(f"source table {src} (queried directly)")

    try:
        total = rows(tquery(f"SELECT COUNT(*) FROM {src}", src, token=args.token))[0][0]
        print(f"source rows: {total}")
    except Exception as e:
        print(f"source rows: (count failed: {e})")

    cols = columns(src, args.token)
    print("\ncolumns (name | type | facet):")
    for c in cols:
        print(f"  {c['name']:32} {(c.get('columnType') or ''):14} {c.get('facetType') or ''}")

    if args.col:
        chosen = [c for c in cols if c["name"] in args.col]
        missing = set(args.col) - {c["name"] for c in cols}
        if missing:
            print(f"\n(warning: --col not found in table: {sorted(missing)})")
    else:
        chosen = [c for c in cols
                  if c.get("facetType") == "enumeration"
                  or (c.get("columnType") or "").endswith("_LIST")]

    names = [c["name"] for c in chosen] or "(none auto-detected — pass --col NAME)"
    print(f"\ndistributions — topical-query SEEDS + expected_pool counts (source ground truth) for {names}:")
    for name, info in distributions(src, chosen, args.token, args.top).items():
        if "error" in info:
            print(f"\n  {name} [{info['type']}]: ERROR {info['error']}")
            continue
        tag = "  (truncated — high cardinality; weak category seed)" if info["truncated"] else ""
        print(f"\n  {name} [{info['type']}]{tag}:")
        for v, n in info["dist"]:
            print(f"      {n:6}  {v}")


if __name__ == "__main__":
    main()
