#!/usr/bin/env python3
"""Data-driven synonym liveness probe for the nf-tools index.

Reads the deployed synonym set's rules and, for each rule, measures how well a query
for the SHORT side (the abbreviation/input) covers the documents that contain the
SPELLED-OUT side, across the synonym-analyzed fields.

  coverage = |docs(abbrev query) ∩ docs(spelled-out phrase)| / |docs(spelled-out phrase)|

If search-time synonym expansion is FIRING, the abbreviation query expands to the phrase
and coverage approaches ~1.0. If it is NOT firing, the abbreviation only matches its own
literal occurrences, so coverage is low (it cannot reach the spelled-out-only docs).

  python3 config/probe_synonyms.py                 # probe the deployed set (id from --set / default 16)
  python3 config/probe_synonyms.py --set 16

Anonymous; queries are read-only. A low coverage on clean multi-word rules (e.g. mpnst,
pnf, cnf) is the signal that the config's search-time synonyms aren't taking effect — see
docs/BACKEND_ISSUES.md #2.
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from query import _call, search, NF_TOOLS, hit_dict

ORG = "org.synapse.nf"
SYN_FIELDS = ["description", "synonyms", "diseaseType", "tumorType", "cellLineManifestation",
              "cellLineGeneticDisorder", "animalModelOfManifestation",
              "animalModelGeneticDisorder", "targetAntigen"]
FIRING_THRESHOLD = 0.8


def _ids(dsl):
    out, frm = set(), 0
    while frm < 3000:
        q = dict(dsl, size=100)
        if frm:
            q["from"] = frm
        r = search(NF_TOOLS, q, response_parts=["HITS", "TOTAL_HITS"])
        hits = r.get("hits", [])
        if not hits:
            break
        for h in hits:
            out.add(hit_dict(h).get("resourceId"))
        frm += len(hits)
        if frm >= r.get("totalHits", 0):
            break
    return out


def term_ids(term):
    return _ids({"query": {"multi_match": {"query": term, "fields": SYN_FIELDS}}})


def phrase_ids(phrase):
    return _ids({"query": {"multi_match": {"query": phrase, "fields": SYN_FIELDS, "type": "phrase"}}})


def parse_rule(rule):
    """Return (probe_term, [target_phrases]). For 'a => b' the probe is the LHS input(s) and
    targets the RHS expansion(s); for equivalent 'a, b, c' the probe is the shortest term and
    targets are the rest (the longer, spelled-out forms)."""
    if "=>" in rule:
        lhs, rhs = rule.split("=>", 1)
        probes = [t.strip() for t in lhs.split(",") if t.strip()]
        targets = [t.strip() for t in rhs.split(",") if t.strip()]
        return probes[0], targets
    terms = [t.strip() for t in rule.split(",") if t.strip()]
    if len(terms) < 2:
        return (terms[0] if terms else ""), []
    probe = min(terms, key=len)
    return probe, [t for t in terms if t != probe]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=int, default=16, help="synonym set id (default 16)")
    args = ap.parse_args()

    _, full = _call(f"search/synonym/set/{args.set}")
    rules = (full.get("definition") or {}).get("synonyms") or []
    print(f"Synonym set {args.set} '{full.get('name')}' — {len(rules)} rules; "
          f"probing over fields: {', '.join(SYN_FIELDS)}\n")
    print(f"{'probe':8}{'-> spelled-out':40}{'#phrase':>8}{'#covered':>9}{'cover%':>8}  verdict")

    not_firing = 0
    for rule in rules:
        probe, targets = parse_rule(rule)
        if not targets:
            print(f"{probe:8}{'(no spelled-out target)':40}{'':>8}{'':>9}{'':>8}  skipped")
            continue
        target_docs = set()
        for t in targets:
            target_docs |= phrase_ids(t)
        probe_docs = term_ids(probe)
        covered = target_docs & probe_docs
        cov = (len(covered) / len(target_docs)) if target_docs else None
        label = ", ".join(targets)[:38]
        if cov is None:
            verdict = "n/a (phrase absent)"
        elif cov >= FIRING_THRESHOLD:
            verdict = "FIRING"
        else:
            verdict = "NOT FIRING"
            not_firing += 1
        covpct = "   -  " if cov is None else f"{cov*100:5.0f}%"
        print(f"{probe:8}{label:40}{len(target_docs):>8}{len(covered):>9}{covpct:>8}  {verdict}")

    print(f"\n{not_firing} rule(s) NOT FIRING (coverage < {FIRING_THRESHOLD:.0%}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
