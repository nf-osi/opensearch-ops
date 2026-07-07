#!/usr/bin/env python3
"""Manage org.synapse.nf search config objects: apply, list, and check bindings.

Subcommands:
  apply   Create/update the nf-tools config objects (all org.synapse.nf objects
          are upserted by name — POST to create, PUT /{id} with the current
          etag to update) and bind the resulting SearchConfiguration to a
          SearchIndex:
            1. TextAnalyzer        nf_scientific_synonyms   (nf_scientific_synonyms.analyzer.json)
            2. ColumnAnalyzerOverride nf_tools_columns       (nf_tools_columns.override.json)
            3. SearchConfiguration nf_tools_search_config    (nf_tools_search_config.json)
            4. Bind the config to the target SearchIndex    (PUT /entity/{id}/searchconfig/binding)
            5. (--rebuild) Touch the SearchIndex entity      (PUT /entity/{id}) to fire a full rebuild
          Bind target defaults to the nf-tools index (syn75081636) but can be
          overridden with --index, passing either a SearchIndex synId or its
          name (resolved by looking it up among the SearchIndex children of
          the shared collection project syn74909065). Binding higher (e.g.
          the shared collection project itself) would resolve down to every
          portal's index, so --index must always name a SearchIndex object,
          not a project/folder.
  list    List registered org.synapse.nf config objects (TextAnalyzer,
          ColumnAnalyzerOverride, SearchConfiguration), optionally filtered
          by --type. Anonymous — no token needed.
  check   Check what config (if any) is bound to given entities — resolves
          up the entity hierarchy — and list configs available to bind for
          the org. Anonymous — no token needed.

Auth (apply only): needs a Sage employee / org-admin token with modify scope.
Reads it from $NF_SERVICE_TOKEN, else the `NF_SERVICE_TOKEN=` line in
~/.bashrc, else --token.

Usage:
  python3 config/config.py apply --dry-run          # print what would happen, no writes
  python3 config/config.py apply                     # create/update + bind (no rebuild)
  python3 config/config.py apply --rebuild           # ... and trigger the index rebuild
  python3 config/config.py apply --staging           # same, but against the staging repo API
  python3 config/config.py apply --index nf-datasets # bind/rebuild a different index, by name

  python3 config/config.py list                            # list all org.synapse.nf configs
  python3 config/config.py list --type SearchConfiguration # ... filtered to one type

  python3 config/config.py check                          # nf-tools index + its source table
  python3 config/config.py check syn75081636 syn51730943  # explicit entity ids
  python3 config/config.py check --org org.synapse.nf
"""
import argparse, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from query import _call, BASE, STAGING_BASE  # _call(ep, method="GET", body=None, token=None, base=None) -> (code, dict)

DEFAULT_INDEX = "syn75081636"  # nf-tools SearchIndex
NF_TOOLS_SOURCE = "syn51730943"  # nf-tools definingSQL source table
SEARCH_INDEX_COLLECTION = "syn74909065"  # project holding all portal SearchIndex objects
SYN_ID_RE = re.compile(r"^syn\d+$", re.IGNORECASE)
ORG = "org.synapse.nf"
HERE = Path(__file__).resolve().parent

OBJECTS = [
    # (type, artifact file, create endpoint, list endpoint, item-path prefix)
    ("TextAnalyzer", "nf_scientific_synonyms.analyzer.json", "search/text/analyzer",
     "search/text/analyzer/list", "search/text/analyzer"),
    ("ColumnAnalyzerOverride", "nf_tools_columns.override.json", "search/column/analyzer/override",
     "search/column/analyzer/override/list", "search/column/analyzer/override"),
    ("SearchConfiguration", "nf_tools_search_config.json", "search/configuration",
     "search/configuration/list", "search/configuration"),
]

TYPE_LIST_EP = {type_: list_ep for type_, _, _, list_ep, _ in OBJECTS}


def get_token(cli_token):
    if cli_token:
        return cli_token
    if os.environ.get("NF_SERVICE_TOKEN"):
        return os.environ["NF_SERVICE_TOKEN"]
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        for line in bashrc.read_text().splitlines():
            if line.startswith("NF_SERVICE_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit("No token: set $NF_SERVICE_TOKEN or pass --token")


def find_existing(list_ep, name, token, base):
    """Return the existing object dict for `name` in ORG, or None."""
    _, body = _call(list_ep, "POST", {"organizationName": ORG}, token, base=base)
    for r in body.get("results", []):
        if r.get("name") == name:
            return r
    return None


def upsert(artifact, create_ep, list_ep, item_ep, token, dry, base):
    spec = json.loads((HERE / artifact).read_text())
    name = spec["name"]
    existing = find_existing(list_ep, name, token, base)
    if existing:
        spec["id"] = existing["id"]
        spec["etag"] = existing["etag"]
        action, ep, method = "UPDATE", f"{item_ep}/{existing['id']}", "PUT"
    else:
        action, ep, method = "CREATE", create_ep, "POST"
    print(f"  {action} {name} -> {method} /{ep}")
    if dry:
        return existing["id"] if existing else "(new)"
    code, body = _call(ep, method, spec, token, base=base)
    if code >= 400:
        sys.exit(f"  FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok id={body.get('id')} etag={body.get('etag')}")
    return body["id"]


def list_configs(token, base, org=ORG, type_filter=None):
    """Return {type: [config dicts]} for the given org, across all three config
    types or just `type_filter` (one of TYPE_LIST_EP's keys)."""
    types = [type_filter] if type_filter else list(TYPE_LIST_EP)
    result = {}
    for t in types:
        _, body = _call(TYPE_LIST_EP[t], "POST", {"organizationName": org}, token, base=base)
        result[t] = body.get("results", [])
    return result


def resolve_index_id(target, token, base):
    """Resolve --index to a SearchIndex synId.

    `target` is returned as-is if it already looks like a synId; otherwise
    it's treated as a SearchIndex name and looked up among the children of
    the shared collection project SEARCH_INDEX_COLLECTION.
    """
    if SYN_ID_RE.match(target):
        return target
    next_page_token = None
    while True:
        payload = {"parentId": SEARCH_INDEX_COLLECTION, "includeTypes": ["searchindex"]}
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        code, body = _call("entity/children", "POST", payload, token, base=base)
        if code >= 400:
            sys.exit(f"  could not list SearchIndex children ({code}): {json.dumps(body)[:500]}")
        for r in body.get("page", []):
            if r.get("name") == target:
                return r["id"]
        next_page_token = body.get("nextPageToken")
        if not next_page_token:
            break
    sys.exit(f"  no SearchIndex named '{target}' found under {SEARCH_INDEX_COLLECTION}")


def bind(config_id, index_id, token, dry, base):
    print(f"  BIND config {config_id} -> {index_id}")
    if dry:
        return
    payload = {"entityId": index_id, "searchConfigurationId": str(config_id)}
    code, body = _call(f"entity/{index_id}/searchconfig/binding", "PUT", payload, token, base=base)
    if code >= 400:
        sys.exit(f"  BIND FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok: {json.dumps(body)[:300]}")


def rebuild(index_id, token, dry, base):
    print(f"  REBUILD: touch entity {index_id} (PUT /entity)")
    code, ent = _call(f"entity/{index_id}", token=token, base=base)
    if code >= 400:
        sys.exit(f"  could not GET entity ({code}): {ent}")
    if dry:
        print(f"    would PUT entity back with etag={ent.get('etag')}")
        return
    code, body = _call(f"entity/{index_id}", "PUT", ent, token, base=base)
    if code >= 400:
        sys.exit(f"  REBUILD PUT FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok: entity updated, etag={body.get('etag')} (rebuild runs async)")


def get_binding(entity_id, base):
    """Return (binding_dict_or_None, message). binding is None when unbound.

    The binding endpoint returns a binding record (bindId, searchConfigurationId,
    objectId, objectType), not the SearchConfiguration itself — fetch that
    separately with get_config().
    """
    code, body = _call(f"entity/{entity_id}/searchconfig/binding", base=base)
    if code >= 400:
        return None, body.get("reason", str(body))
    return body, "bound"


def get_config(config_id, base):
    code, body = _call(f"search/configuration/{config_id}", base=base)
    return body if code < 400 else None


def describe_config(cfg):
    print(f"    id={cfg.get('id')}  name={cfg.get('name')}  org={cfg.get('organizationName')}")
    if cfg.get("description"):
        print(f"    desc: {cfg['description']}")
    # Field/analyzer wiring lives in the config body; print whatever keys exist
    # beyond the standard metadata so we don't assume a fixed schema.
    meta = {"id", "organizationName", "name", "description", "etag",
            "createdOn", "createdBy", "modifiedOn", "modifiedBy", "concreteType"}
    extra = {k: v for k, v in cfg.items() if k not in meta}
    if extra:
        for k, v in extra.items():
            print(f"    {k}: {v}")


def cmd_apply(args):
    base = STAGING_BASE if args.staging else BASE
    token = get_token(args.token)
    index_id = resolve_index_id(args.index, token, base)

    print(f"Target: {base}  index={index_id}")
    print("Upserting org.synapse.nf search objects:")
    config_id = None
    for _type, artifact, create_ep, list_ep, item_ep in OBJECTS:
        config_id = upsert(artifact, create_ep, list_ep, item_ep, token, args.dry_run, base)

    print("\nBinding:")
    bind(config_id, index_id, token, args.dry_run, base)

    if args.rebuild:
        print("\nRebuild:")
        rebuild(index_id, token, args.dry_run, base)
    else:
        print("\n(skip rebuild; re-run with --rebuild to apply the config to the live index)")

    print("\nDone." + ("  [DRY RUN — no writes made]" if args.dry_run else ""))


def cmd_list(args):
    base = STAGING_BASE if args.staging else BASE
    print(f"Configs registered for org '{args.org}'" + (f" (type={args.type})" if args.type else "")
          + f"  [{base}]")
    for t, items in list_configs(args.token, base, org=args.org, type_filter=args.type).items():
        print(f"\n{t}:")
        if not items:
            print("  (none)")
        for c in items:
            print(f"  id={c.get('id')}  name={c.get('name')}")


def cmd_check(args):
    base = STAGING_BASE if args.staging else BASE
    entities = args.entities or [DEFAULT_INDEX, NF_TOOLS_SOURCE]

    print("Binding check (resolves up the entity hierarchy):")
    any_bound = False
    for ent in entities:
        binding, msg = get_binding(ent, base)
        if binding is None:
            print(f"  {ent}: UNBOUND — {msg}")
            continue
        any_bound = True
        print(f"  {ent}: BOUND →")
        config_id = binding.get("searchConfigurationId")
        cfg = get_config(config_id, base) if config_id else None
        if cfg is None:
            print(f"    could not fetch SearchConfiguration {config_id}; raw binding:")
            for k, v in binding.items():
                print(f"    {k}: {v}")
        else:
            describe_config(cfg)

    print(f"\nConfigs registered for org '{args.org}':")
    configs = list_configs(None, base, org=args.org, type_filter="SearchConfiguration")["SearchConfiguration"]
    if not configs:
        print("  (none)")
    else:
        for c in configs:
            print(f"  id={c['id']}  name={c['name']}")

    if not any_bound:
        print("\nResult: NO config bound → index uses platform default analyzers"
              " (pure query-time behavior, which is what the benchmark measures).")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    ap_apply = sub.add_parser("apply", help="create/update config objects and bind to a SearchIndex")
    ap_apply.add_argument("--token")
    ap_apply.add_argument("--dry-run", action="store_true")
    ap_apply.add_argument("--rebuild", action="store_true",
                           help="touch the SearchIndex entity to fire a full rebuild")
    ap_apply.add_argument("--staging", action="store_true",
                           help=f"hit the staging repo API ({STAGING_BASE}) instead of prod, for testing")
    ap_apply.add_argument("--index", default=DEFAULT_INDEX,
                           help=f"SearchIndex to bind/rebuild — a synId or a name looked up under "
                                f"{SEARCH_INDEX_COLLECTION} (default: {DEFAULT_INDEX}, nf-tools)")
    ap_apply.set_defaults(func=cmd_apply)

    ap_list = sub.add_parser("list", help="list registered org config objects")
    ap_list.add_argument("--token")
    ap_list.add_argument("--staging", action="store_true",
                          help=f"hit the staging repo API ({STAGING_BASE}) instead of prod")
    ap_list.add_argument("--org", default=ORG, help="org to list configs for")
    ap_list.add_argument("--type", choices=sorted(TYPE_LIST_EP), help="restrict to one config type")
    ap_list.set_defaults(func=cmd_list)

    ap_check = sub.add_parser("check", help="check what config is bound to given entities")
    ap_check.add_argument("entities", nargs="*", help="entity ids to check (default: nf-tools + source)")
    ap_check.add_argument("--staging", action="store_true",
                           help=f"hit the staging repo API ({STAGING_BASE}) instead of prod")
    ap_check.add_argument("--org", default=ORG, help="org to list configs for")
    ap_check.set_defaults(func=cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
