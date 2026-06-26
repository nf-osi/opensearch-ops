#!/usr/bin/env python3
"""Check what search configuration (if any) is in effect for an NF index.

For each entity given, asks the binding endpoint
`GET /entity/{id}/searchconfig/binding` (which resolves up the entity
hierarchy). If a config is bound, fetches it and prints its referenced text
analyzers / synonym sets. Also lists the configs registered for the org so you
can see what's available to bind.

Anonymous — no token needed (these objects are public).

Usage:
  python3 check_config.py                          # nf-tools index + its source table
  python3 check_config.py syn75081636 syn51730943  # explicit entity ids
  python3 check_config.py --org org.synapse.nf
"""
import argparse, sys
from query import _call, NF_TOOLS

NF_TOOLS_SOURCE = "syn51730943"  # nf-tools definingSQL source table
DEFAULT_ORG = "org.synapse.nf"


def get_binding(entity_id):
    """Return (config_dict_or_None, message). config is None when unbound."""
    code, body = _call(f"entity/{entity_id}/searchconfig/binding")
    if code >= 400:
        return None, body.get("reason", str(body))
    return body, "bound"


def get_config(config_id):
    code, body = _call(f"search/configuration/{config_id}")
    return body if code < 400 else None


def list_configs(org=None):
    body = {"organizationName": org} if org else {}
    _, res = _call("search/configuration/list", "POST", body)
    return res.get("results", [])


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entities", nargs="*", help="entity ids to check (default: nf-tools + source)")
    ap.add_argument("--org", default=DEFAULT_ORG, help="org to list configs for")
    args = ap.parse_args()

    entities = args.entities or [NF_TOOLS, NF_TOOLS_SOURCE]

    print("Binding check (resolves up the entity hierarchy):")
    any_bound = False
    for ent in entities:
        cfg, msg = get_binding(ent)
        if cfg is None:
            print(f"  {ent}: UNBOUND — {msg}")
        else:
            any_bound = True
            print(f"  {ent}: BOUND →")
            describe_config(cfg)

    print(f"\nConfigs registered for org '{args.org}':")
    nf = list_configs(args.org)
    if not nf:
        print("  (none)")
    else:
        for c in nf:
            print(f"  id={c['id']}  name={c['name']}")

    if not any_bound:
        print("\nResult: NO config bound → index uses platform default analyzers"
              " (pure query-time behavior, which is what the benchmark measures).")
    return 0 if True else 1


if __name__ == "__main__":
    sys.exit(main())
