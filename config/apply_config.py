#!/usr/bin/env python3
"""Create/update the nf-tools SearchConfiguration and bind it to the index.

The SynonymSet the analyzer references (org.synapse.nf-standard_synonyms) is NOT managed
here — it is maintained in the nf-metadata-dictionary repo; this pipeline assumes it
already exists in org.synapse.nf.

Pipeline (all org.synapse.nf objects are upserted by name — POST to create,
PUT /{id} with the current etag to update):

  1. TextAnalyzer        nf_scientific_synonyms   (config/nf_scientific_synonyms.analyzer.json)
  2. ColumnAnalyzerOverride nf_tools_columns       (config/nf_tools_columns.override.json)
  3. SearchConfiguration nf_tools_search_config    (config/nf_tools_search_config.json)
  4. Bind the config to the nf-tools SearchIndex   (PUT /entity/syn75081636/searchconfig/binding)
  5. (--rebuild) Touch the SearchIndex entity      (PUT /entity/syn75081636) to fire a full rebuild

Bind target is hard-coded to the nf-tools index only (syn75081636); a config bound
higher (e.g. the shared collection project) would resolve down to every portal's index.

Auth: needs a Sage employee / org-admin token with modify scope. Reads it from
$NF_SERVICE_TOKEN, else from the `NF_SERVICE_TOKEN=` line in ~/.bashrc, else --token.

Usage:
  python3 config/apply_config.py --dry-run          # print what would happen, no writes
  python3 config/apply_config.py                     # create/update + bind (no rebuild)
  python3 config/apply_config.py --rebuild           # ... and trigger the index rebuild
"""
import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from query import _call  # (ep, method="GET", body=None, token=None) -> (code, dict)

NF_TOOLS_INDEX = "syn75081636"
ORG = "org.synapse.nf"
HERE = Path(__file__).resolve().parent

OBJECTS = [
    # (artifact file, create endpoint, list endpoint, item-path prefix)
    # NOTE: the SynonymSet that the analyzer $refs (org.synapse.nf-standard_synonyms) is
    # deliberately NOT managed here — it is maintained in the nf-metadata-dictionary repo.
    # This pipeline assumes it already exists in org.synapse.nf.
    ("nf_scientific_synonyms.analyzer.json", "search/text/analyzer",
     "search/text/analyzer/list", "search/text/analyzer"),
    ("nf_tools_columns.override.json", "search/column/analyzer/override",
     "search/column/analyzer/override/list", "search/column/analyzer/override"),
    ("nf_tools_search_config.json", "search/configuration",
     "search/configuration/list", "search/configuration"),
]


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


def find_existing(list_ep, name, token):
    """Return the existing object dict for `name` in ORG, or None."""
    _, body = _call(list_ep, "POST", {"organizationName": ORG}, token)
    for r in body.get("results", []):
        if r.get("name") == name:
            return r
    return None


def upsert(artifact, create_ep, list_ep, item_ep, token, dry):
    spec = json.loads((HERE / artifact).read_text())
    name = spec["name"]
    existing = find_existing(list_ep, name, token)
    if existing:
        spec["id"] = existing["id"]
        spec["etag"] = existing["etag"]
        action, ep, method = "UPDATE", f"{item_ep}/{existing['id']}", "PUT"
    else:
        action, ep, method = "CREATE", create_ep, "POST"
    print(f"  {action} {name} -> {method} /{ep}")
    if dry:
        return existing["id"] if existing else "(new)"
    code, body = _call(ep, method, spec, token)
    if code >= 400:
        sys.exit(f"  FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok id={body.get('id')} etag={body.get('etag')}")
    return body["id"]


def bind(config_id, token, dry):
    print(f"  BIND config {config_id} -> {NF_TOOLS_INDEX}")
    if dry:
        return
    payload = {"entityId": NF_TOOLS_INDEX, "searchConfigurationId": str(config_id)}
    code, body = _call(f"entity/{NF_TOOLS_INDEX}/searchconfig/binding", "PUT", payload, token)
    if code >= 400:
        sys.exit(f"  BIND FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok: {json.dumps(body)[:300]}")


def rebuild(token, dry):
    print(f"  REBUILD: touch entity {NF_TOOLS_INDEX} (PUT /entity)")
    code, ent = _call(f"entity/{NF_TOOLS_INDEX}", token=token)
    if code >= 400:
        sys.exit(f"  could not GET entity ({code}): {ent}")
    if dry:
        print(f"    would PUT entity back with etag={ent.get('etag')}")
        return
    code, body = _call(f"entity/{NF_TOOLS_INDEX}", "PUT", ent, token)
    if code >= 400:
        sys.exit(f"  REBUILD PUT FAILED ({code}): {json.dumps(body)[:500]}")
    print(f"    ok: entity updated, etag={body.get('etag')} (rebuild runs async)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="touch the SearchIndex entity to fire a full rebuild")
    args = ap.parse_args()
    token = get_token(args.token)

    print("Upserting org.synapse.nf search objects:")
    config_id = None
    for artifact, create_ep, list_ep, item_ep in OBJECTS:
        config_id = upsert(artifact, create_ep, list_ep, item_ep, token, args.dry_run)

    print("\nBinding:")
    bind(config_id, token, args.dry_run)

    if args.rebuild:
        print("\nRebuild:")
        rebuild(token, args.dry_run)
    else:
        print("\n(skip rebuild; re-run with --rebuild to apply the config to the live index)")

    print("\nDone." + ("  [DRY RUN — no writes made]" if args.dry_run else ""))


if __name__ == "__main__":
    main()
