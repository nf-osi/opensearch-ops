#!/usr/bin/env python3
"""Verify the local nf-tools search-config artifacts against prod and for internal consistency.

Checks (anonymous — all objects are public):
  1. DRIFT      — each local artifact's substantive fields match the deployed object of the
                  same name (ignoring server-managed id/etag/timestamps).
  2. REFS       — every `$ref` resolves: built-in `org.sagebionetworks-*` analyzers are
                  assumed valid; `org.synapse.nf-*` refs must exist as registered objects.
  3. COLUMNS    — every column named in the override exists in the live index mapping.
  4. BINDING    — the config is bound to the nf-tools index (syn75081636) and nothing higher.
  5. SYNONYMS   — the synonym set the analyzer references exists and is non-empty. This set
                  (org.synapse.nf-standard_synonyms) is NOT managed in this repo — it is
                  maintained in nf-metadata-dictionary; this is a dependency check only.

Exit code is non-zero if any check fails. This validates the artifacts are correct; it does
NOT prove search-time behavior (see docs/BACKEND_ISSUES.md issue 2).
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from query import _call, search, NF_TOOLS, hit_dict

HERE = Path(__file__).resolve().parent
ORG = "org.synapse.nf"
SERVER_FIELDS = {"id", "etag", "createdOn", "createdBy", "modifiedOn", "modifiedBy", "concreteType"}

# (artifact file, list endpoint, item endpoint)
ARTIFACTS = [
    ("nf_scientific_synonyms.analyzer.json", "search/text/analyzer/list", "search/text/analyzer"),
    ("nf_tools_columns.override.json", "search/column/analyzer/override/list", "search/column/analyzer/override"),
    ("nf_tools_search_config.json", "search/configuration/list", "search/configuration"),
]

ok = True
def check(cond, msg):
    global ok
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        ok = False

def find(list_ep, name):
    _, body = _call(list_ep, "POST", {"organizationName": ORG})
    for r in body.get("results", []):
        if r.get("name") == name:
            return r
    return None

def substantive(d):
    return {k: v for k, v in d.items() if k not in SERVER_FIELDS}

def collect_refs(obj, acc):
    if isinstance(obj, dict):
        if "$ref" in obj and isinstance(obj["$ref"], str):
            acc.add(obj["$ref"])
        for v in obj.values():
            collect_refs(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_refs(v, acc)
    return acc


print("1. DRIFT (local artifact vs deployed object)")
deployed = {}
for fname, list_ep, _ in ARTIFACTS:
    local = json.loads((HERE / fname).read_text())
    dep = find(list_ep, local["name"])
    deployed[local["name"]] = dep
    if dep is None:
        check(False, f"{local['name']}: not found in prod")
        continue
    check(substantive(dep) == local, f"{local['name']}: deployed matches local")

print("\n2. REFS ($ref resolution)")
refs = set()
for fname, _, _ in ARTIFACTS:
    collect_refs(json.loads((HERE / fname).read_text()), refs)
# Registered org.synapse.nf objects, by namespaced key, across all object types.
registered = set()
for ep in ("search/text/analyzer/list", "search/column/analyzer/override/list",
           "search/synonym/set/list", "search/configuration/list"):
    _, body = _call(ep, "POST", {"organizationName": ORG})
    for r in body.get("results", []):
        registered.add(f"{ORG}-{r['name']}")
for ref in sorted(refs):
    if ref.startswith("org.sagebionetworks-"):
        check(True, f"{ref}: built-in (assumed valid)")
    else:
        check(ref in registered, f"{ref}: registered in {ORG}")

print("\n3. COLUMNS (override columns exist in the live index)")
r = search(NF_TOOLS, {"query": {"match_all": {}}, "size": 1},
           response_parts=["HITS", "SELECT_COLUMNS"])
cols = r.get("selectColumns") or r.get("columns") or []
index_cols = {c.get("name") for c in cols}
override = json.loads((HERE / "nf_tools_columns.override.json").read_text())
for entry in override["overrides"]:
    cn = entry["columnName"]
    check(cn in index_cols, f"column {cn!r} present in index")

print("\n4. BINDING (config -> nf-tools index)")
cfg = deployed.get("nf_tools_search_config")
_, b = _call(f"entity/{NF_TOOLS}/searchconfig/binding")
bound_id = b.get("searchConfigurationId") if isinstance(b, dict) else None
check(cfg is not None and bound_id == str(cfg.get("id")),
      f"syn75081636 bound to config id={cfg.get('id') if cfg else '?'} (got {bound_id})")

print("\n5. SYNONYMS (analyzer's synonym set exists & populated; set maintained in nf-metadata-dictionary)")
analyzer = json.loads((HERE / "nf_scientific_synonyms.analyzer.json").read_text())
syn_refs = collect_refs(analyzer.get("settings", {}).get("filter", {}), set())
syn_refs = {x for x in syn_refs if "synonym" in x.lower() or x.endswith("standard_synonyms")}
_, slist = _call("search/synonym/set/list", "POST", {"organizationName": ORG})
by_key = {f"{ORG}-{s['name']}": s for s in slist.get("results", [])}
for ref in sorted(syn_refs) or ["(none referenced)"]:
    s = by_key.get(ref)
    if s is None:
        check(False, f"{ref}: synonym set not found")
        continue
    _, full = _call(f"search/synonym/set/{s['id']}")
    syns = (full.get("definition") or {}).get("synonyms") or []
    check(len(syns) > 0, f"{ref}: id={s['id']} populated ({len(syns)} rules)")

print()
print("RESULT:", "all checks passed" if ok else "FAILURES above")
sys.exit(0 if ok else 1)
