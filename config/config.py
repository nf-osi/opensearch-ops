#!/usr/bin/env python3
"""Manage org.synapse.nf search config objects: register, apply, list, and check.

Subcommands:
  register  Create/update every org.synapse.nf config object declared by a
          *.json artifact in config/ (every object is upserted by name —
          POST to create, PUT /{id} with the current etag to update). Each
          artifact's type (TextAnalyzer / ColumnAnalyzerOverride /
          SearchConfiguration) is inferred from its shape — "settings" =>
          TextAnalyzer, "overrides" => ColumnAnalyzerOverride, "defaultAnalyzer"
          => SearchConfiguration.
          Registers TextAnalyzers and ColumnAnalyzerOverrides before
          SearchConfigurations, since the latter reference the former by
          name. Prints each resulting SearchConfiguration's id — pass one to
          `apply` to bind it.
  apply   Bind an existing SearchConfiguration (by id — see `list`) to a
          SearchIndex, and optionally rebuild:
            1. Bind the config to the target SearchIndex    (PUT /entity/{id}/searchconfig/binding)
            2. (--rebuild) Touch the SearchIndex entity      (PUT /entity/{id}) to fire a full rebuild
          apply does NOT create/update config objects — run `register` first
          if the config doesn't exist yet. Bind target defaults to the nf-tools
          index (syn75081636) but can be overridden with --index, passing
          either a SearchIndex synId or its name (resolved by looking it up
          among the SearchIndex children of the shared collection project
          syn74909065). Binding higher (e.g. the shared collection project
          itself) would resolve down to every portal's index, so --index
          must always name a SearchIndex object, not a project/folder.
  list    List registered org.synapse.nf config objects (TextAnalyzer,
          ColumnAnalyzerOverride, SearchConfiguration), optionally filtered
          by --type. Anonymous — no token needed.
  check   Check what config (if any) is bound to given entities — resolves
          up the entity hierarchy — and list configs available to bind for
          the org. Anonymous — no token needed.

Auth (register/apply only): needs a Sage employee / org-admin token with modify
scope. Reads it from $NF_SERVICE_TOKEN, else the `NF_SERVICE_TOKEN=` line in
~/.bashrc, else --token.

Usage:
  python3 config/config.py register --dry-run   # print what would happen, no writes
  python3 config/config.py register             # create/update the config objects
  python3 config/config.py register --staging   # same, but against the staging repo API

  python3 config/config.py apply 9                     # bind SearchConfiguration id 9 to nf-tools
  python3 config/config.py apply 9 --rebuild            # ... and trigger the index rebuild
  python3 config/config.py apply 9 --index nf-datasets  # ... to a different index, by name

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

# Each config object type, its REST endpoints, and the JSON key whose presence
# identifies an artifact as that type (checked in this order, first match wins).
TYPE_SPECS = {
    "TextAnalyzer": {
        "discriminator": "settings",
        "create_ep": "search/text/analyzer",
        "list_ep": "search/text/analyzer/list",
        "item_ep": "search/text/analyzer",
    },
    "ColumnAnalyzerOverride": {
        "discriminator": "overrides",
        "create_ep": "search/column/analyzer/override",
        "list_ep": "search/column/analyzer/override/list",
        "item_ep": "search/column/analyzer/override",
    },
    "SearchConfiguration": {
        "discriminator": "defaultAnalyzer",
        "create_ep": "search/configuration",
        "list_ep": "search/configuration/list",
        "item_ep": "search/configuration",
    },
}
# registration order: types with no intra-config dependency first, since a
# SearchConfiguration references a TextAnalyzer/ColumnAnalyzerOverride by name
TYPE_ORDER = ["TextAnalyzer", "ColumnAnalyzerOverride", "SearchConfiguration"]

TYPE_LIST_EP = {t: s["list_ep"] for t, s in TYPE_SPECS.items()}


def infer_type(spec):
    for t in TYPE_ORDER:
        if TYPE_SPECS[t]["discriminator"] in spec:
            return t
    sys.exit(f"  could not infer config type for artifact with keys {sorted(spec.keys())}")


def discover_artifacts():
    """Return config/*.json artifact paths, sorted per TYPE_ORDER so dependencies
    (TextAnalyzer, ColumnAnalyzerOverride) register before the SearchConfigurations
    that reference them by name."""
    paths = sorted(HERE.glob("*.json"))
    return sorted(paths, key=lambda p: TYPE_ORDER.index(infer_type(json.loads(p.read_text()))))


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
    code, body = _call(list_ep, "POST", {"organizationName": ORG}, token, base=base)
    if code >= 400:
        sys.exit(f"  could not list {list_ep} ({code}): {json.dumps(body)[:500]}")
    for r in body.get("results", []):
        if r.get("name") == name:
            return r
    return None


def upsert(artifact_path, token, dry, base):
    spec = json.loads(artifact_path.read_text())
    type_ = infer_type(spec)
    endpoints = TYPE_SPECS[type_]
    name = spec["name"]
    existing = find_existing(endpoints["list_ep"], name, token, base)
    if existing:
        spec["id"] = existing["id"]
        spec["etag"] = existing["etag"]
        action, ep, method = "UPDATE", f"{endpoints['item_ep']}/{existing['id']}", "PUT"
    else:
        action, ep, method = "CREATE", endpoints["create_ep"], "POST"
    print(f"  {action} [{type_}] {name} -> {method} /{ep}")
    if dry:
        return type_, name, (existing["id"] if existing else "(new)")
    code, body = _call(ep, method, spec, token, base=base)
    if code >= 400:
        sys.exit(f"  FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok id={body.get('id')} etag={body.get('etag')}")
    return type_, name, body["id"]


def list_configs(token, base, org=ORG, type_filter=None):
    """Return {type: [config dicts]} for the given org, across all three config
    types or just `type_filter` (one of TYPE_LIST_EP's keys)."""
    types = [type_filter] if type_filter else list(TYPE_LIST_EP)
    result = {}
    for t in types:
        code, body = _call(TYPE_LIST_EP[t], "POST", {"organizationName": org}, token, base=base)
        if code >= 400:
            sys.exit(f"  could not list {t} configs ({code}): {json.dumps(body)[:500]}")
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


def cmd_register(args):
    base = STAGING_BASE if args.staging else BASE
    token = get_token(args.token)

    print(f"Target: {base}")
    print("Registering org.synapse.nf search objects:")
    configs = []
    for artifact_path in discover_artifacts():
        type_, name, result_id = upsert(artifact_path, token, args.dry_run, base)
        if type_ == "SearchConfiguration":
            configs.append((name, result_id))

    print("\nDone." + ("  [DRY RUN — no writes made]" if args.dry_run else ""))
    if configs and not args.dry_run:
        print("SearchConfiguration id(s):")
        for name, cid in configs:
            print(f"  {name}: {cid}  ->  bind with: config.py apply {cid}")


def cmd_apply(args):
    base = STAGING_BASE if args.staging else BASE
    token = get_token(args.token)
    index_id = resolve_index_id(args.index, token, base)

    print(f"Target: {base}  index={index_id}")
    print("\nBinding:")
    bind(args.config_id, index_id, token, args.dry_run, base)

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

    ap_register = sub.add_parser("register", help="create/update every org.synapse.nf config object found in config/*.json")
    ap_register.add_argument("--token")
    ap_register.add_argument("--dry-run", action="store_true")
    ap_register.add_argument("--staging", action="store_true",
                              help=f"hit the staging repo API ({STAGING_BASE}) instead of prod, for testing")
    ap_register.set_defaults(func=cmd_register)

    ap_apply = sub.add_parser("apply", help="bind an existing SearchConfiguration to a SearchIndex")
    ap_apply.add_argument("config_id", help="id of an existing SearchConfiguration (see `config.py list`)")
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
