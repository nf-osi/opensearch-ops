#!/usr/bin/env python3
"""Validate the golden relevance sets: schema/format, plus duplicate queries and ids.

Run by the `validate-goldens` GitHub Actions workflow on any change to a golden.yaml.
Offline — parses YAML only, never queries Synapse, so it needs no credentials.

WHAT IT CHECKS

1. Duplicate queries (the headline check). run.py sends `case["query"]` to the index and
   scores the ranked ids against `case["relevant"]`. Two cases with the same query issue
   the SAME request, so they cannot disagree about ranking, only about ground truth. That
   double-counts one query in every aggregate (MRR / Recall@k / Hit@1 / Hit@k are plain
   means over cases), silently reweighting the benchmark toward whatever that query
   measures. It is also how a golden drifts: the same user question gets curated twice,
   months apart, with two different `relevant` sets, and both cases "pass".

   Queries are compared case-insensitively with runs of whitespace collapsed, because the
   index lowercases at analysis time — `NTAP MRI` and `ntap mri` are the same request.
   Punctuation is deliberately NOT normalized: `mpnst rna-seq` and `mpnst rna seq` analyze
   differently (word_delimiter), so they are genuinely distinct queries a golden may hold
   both of. Likewise `ntap mri` vs `NTAP AND MRI` are distinct on purpose.

2. Duplicate case ids. run.py writes per-case results into a dict keyed on `case["id"]`,
   so a repeated id makes one case's scores silently overwrite the other's while the
   aggregates still count both.

3. Schema / format. Required keys present, no unknown keys (this is what catches a
   `revewer:` or `expected_pools:` typo, which would otherwise be silently ignored by the
   runner forever), values of the right type, and a few cross-field invariants:
   `expected_pool` cannot be smaller than the `relevant` head it is a pool for, ids inside
   `relevant` must be unique (a repeat inflates the recall denominator), `source_url`
   needs a `source`, and `last_reviewed` cannot be in the future.

   MOST CASE FIELDS ARE OPTIONAL — only `id`, `query` and `relevant` are required. In
   particular `last_reviewed` / `reviewer` / `source` / `type` / `expected_pool` are all
   optional, so older or hand-seeded cases that predate a convention stay valid. Adding a
   new field means adding it to CASE_OPTIONAL below; that is deliberate, so a typo can't
   masquerade as a new field.

Usage:
  python3 benchmark/validate_goldens.py                    # all benchmark/*/golden.yaml
  python3 benchmark/validate_goldens.py path/to/golden.yaml [...]

Exit status: 0 = clean, 1 = at least one problem found. Under GitHub Actions it also
emits ::error file=,line= annotations so failures land on the diff.
"""
import datetime
import glob
import os
import re
import sys
from collections import Counter, defaultdict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

# --- schema -----------------------------------------------------------------
# Values are the accepted Python type after yaml.safe_load. `version` is a str because
# "2026.07.30" has two dots; a one-dot value like 2026.07 would load as a float and is
# rejected, since it would silently reformat on any round-trip.
TOP_REQUIRED = {
    "index": str,        # SearchIndex synId the cases are scored against
    "index_name": str,
    "version": str,
    "k": int,            # default cutoff for Recall@k / Hit@k
    "id_field": str,     # which column `relevant` ids come from ("rowId" for the index's own)
    "cases": list,
}
TOP_OPTIONAL = {
    "notes": str,        # free-text provenance / conventions block
}

CASE_REQUIRED = {
    "id": str,
    "query": str,
    "relevant": list,
}
CASE_OPTIONAL = {
    "type": str,                       # known-item | topical
    "source": str,                     # where the case came from
    "source_url": str,                 # document backing `source`
    "notes": str,
    "expected_pool": int,              # SME estimate of the TRUE relevant count in the source
    "entity_types": list,              # kinds of thing the expected results are
    "recall_gap_current": bool,        # Hit@20 flag against the live index
    "recall_gap_legacy_search": bool,  # the legacy MySQL search found none
    "last_reviewed": datetime.date,    # OPTIONAL: when an SME last blessed the ground truth
    "reviewer": str,                   # OPTIONAL: who did
}
CASE_TYPES = {"known-item", "topical"}


def type_name(t):
    return {str: "a string", int: "an integer", bool: "a boolean", list: "a list",
            datetime.date: "a date (YYYY-MM-DD, unquoted)"}.get(t, t.__name__)


def type_ok(value, expected):
    """isinstance with two YAML-specific corrections.

    bool is a subclass of int in Python, so `expected_pool: true` would sail through a
    bare isinstance(value, int) check. And datetime.datetime is a subclass of date, so a
    `last_reviewed: 2026-07-30 12:00:00` would pass as a date — reject it, dates here are
    day-grained.
    """
    if expected is int and isinstance(value, bool):
        return False
    if expected is datetime.date and isinstance(value, datetime.datetime):
        return False
    return isinstance(value, expected)


def normalize(query):
    """Collapse a query to the form the index actually sees: lowercased, single-spaced."""
    return " ".join(str(query).lower().split())


def case_lines(path):
    """Map case id -> [1-based line numbers of its `- id:` lines], for CI annotations.

    Read as text rather than via yaml so the numbers point at the real file. A list, not a
    single line, so a repeated id can be annotated at every occurrence. Ids are matched on
    the `  - id: <value>` form every golden uses; anything unparsed falls back to line 1.
    """
    lines = defaultdict(list)
    with open(path) as fh:
        for n, line in enumerate(fh, start=1):
            m = re.match(r"\s*-\s+id:\s*(\S+)", line)
            if m:
                lines[m.group(1).strip("\"'")].append(n)
    return lines


def annotate(path, line, message):
    """Emit a GitHub Actions error annotation, if we're running in one."""
    if not IN_ACTIONS:
        return
    root = os.path.dirname(HERE)
    rel = os.path.relpath(path, root)
    if rel.startswith(".."):  # outside the repo (local ad-hoc run); keep the path as given
        rel = path
    print(f"::error file={rel},line={line}::{message}")


def check_top_level(doc):
    """Schema-check the golden's top-level mapping. Returns a list of messages."""
    problems = []
    for key, want in TOP_REQUIRED.items():
        if key not in doc:
            problems.append(f"missing required top-level key `{key}`")
        elif not type_ok(doc[key], want):
            problems.append(f"top-level `{key}` must be {type_name(want)}, "
                            f"got {doc[key]!r}")
    known = set(TOP_REQUIRED) | set(TOP_OPTIONAL)
    for key in doc:
        if key not in known:
            problems.append(f"unknown top-level key `{key}` — fix the typo, or add it to "
                            f"TOP_OPTIONAL in benchmark/validate_goldens.py if intended")
    if type_ok(doc.get("k"), int) and doc["k"] < 1:
        problems.append(f"top-level `k` must be >= 1, got {doc['k']}")
    return problems


def check_case(case, pos, today):
    """Schema-check one case. Returns (case_id, [messages])."""
    problems = []
    if not isinstance(case, dict):
        return None, [f"case #{pos} is not a mapping (got {type(case).__name__})"]

    cid = case.get("id") if isinstance(case.get("id"), str) else None
    label = f"`{cid}`" if cid else f"case #{pos}"

    for key, want in CASE_REQUIRED.items():
        if key not in case:
            problems.append(f"{label}: missing required key `{key}`")
        elif not type_ok(case[key], want):
            problems.append(f"{label}: `{key}` must be {type_name(want)}, got {case[key]!r}")

    known = set(CASE_REQUIRED) | set(CASE_OPTIONAL)
    for key in case:
        if key not in known:
            problems.append(f"{label}: unknown key `{key}` — fix the typo, or add it to "
                            f"CASE_OPTIONAL in benchmark/validate_goldens.py if intended")
    for key, want in CASE_OPTIONAL.items():
        if key in case and not type_ok(case[key], want):
            problems.append(f"{label}: `{key}` must be {type_name(want)}, got {case[key]!r}")

    if isinstance(case.get("id"), str) and not case["id"].strip():
        problems.append(f"{label}: `id` is blank")
    if isinstance(case.get("query"), str) and not case["query"].strip():
        problems.append(f"{label}: `query` is blank")

    if isinstance(case.get("type"), str) and case["type"] not in CASE_TYPES:
        problems.append(f"{label}: `type` must be one of {sorted(CASE_TYPES)}, "
                        f"got {case['type']!r}")

    rel = case.get("relevant")
    if isinstance(rel, list):
        if not rel:
            problems.append(f"{label}: `relevant` is empty — a case with no relevant ids "
                            f"scores 0 on every strategy and only drags the aggregates down")
        bad = [r for r in rel if not isinstance(r, str) or not r.strip()]
        if bad:
            problems.append(f"{label}: `relevant` entries must be non-blank strings; "
                            f"got {bad!r}")
        dupes = sorted(r for r, n in Counter(rel).items() if n > 1)
        if dupes:
            problems.append(f"{label}: `relevant` repeats {dupes!r} — a repeat inflates the "
                            f"Recall@k denominator, so the case can never score 1.0")

    pool = case.get("expected_pool")
    if type_ok(pool, int):
        if pool < 1:
            problems.append(f"{label}: `expected_pool` must be >= 1, got {pool}")
        elif isinstance(rel, list) and pool < len(rel):
            problems.append(f"{label}: `expected_pool` is {pool} but `relevant` lists "
                            f"{len(rel)} ids — the pool is the TRUE relevant count in the "
                            f"source, so it cannot be smaller than the head")

    if case.get("source_url") and not case.get("source"):
        problems.append(f"{label}: has `source_url` but no `source` — name the source so "
                        f"the case can be grouped with its batch")

    reviewed = case.get("last_reviewed")
    if type_ok(reviewed, datetime.date) and reviewed > today:
        problems.append(f"{label}: `last_reviewed` is {reviewed}, which is in the future "
                        f"(today is {today}) — a review cannot have happened yet")

    return cid, problems


def check(path, today=None):
    """Return a list of human-readable problems found in one golden file."""
    today = today or datetime.date.today()
    try:
        doc = yaml.safe_load(open(path)) or {}
    except yaml.YAMLError as exc:
        return [f"{path}: not valid YAML: {exc}"]
    if not isinstance(doc, dict):
        return [f"{path}: top level must be a mapping, got {type(doc).__name__}"]

    problems = [f"{path}: {m}" for m in check_top_level(doc)]

    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        problems.append(f"{path}: no `cases:` list found")
        return problems

    lines = case_lines(path)
    by_query = defaultdict(list)
    by_id = Counter()

    for pos, case in enumerate(cases, start=1):
        cid, case_problems = check_case(case, pos, today)
        problems.extend(f"{path}: {m}" for m in case_problems)
        for m in case_problems:
            for line in (lines.get(cid) or [1]) if cid else [1]:
                annotate(path, line, m)
        if not isinstance(case, dict):
            continue
        if cid:
            by_id[cid] += 1
        if isinstance(case.get("query"), str):
            by_query[normalize(case["query"])].append(
                (cid or f"<case #{pos}>", case["query"]))

    for norm, group in sorted(by_query.items()):
        if len(group) < 2:
            continue
        shown = ", ".join(f"`{cid}` (query: {raw!r})" for cid, raw in group)
        problems.append(
            f"{path}: query {norm!r} is used by {len(group)} cases: {shown}. "
            f"Merge them into one case, or change one query so the two cases measure "
            f"different requests.")
        for cid, _ in group:
            others = ", ".join(o for o, _ in group if o != cid)
            for line in lines.get(cid) or [1]:
                annotate(path, line, f"duplicate query {norm!r} — also used by {others}")

    for cid, n in sorted(by_id.items()):
        if n > 1:
            problems.append(f"{path}: case id `{cid}` is used {n} times; ids must be "
                            f"unique (run.py keys per-case results on them)")
            for line in lines.get(cid) or [1]:
                annotate(path, line, f"duplicate case id `{cid}`")

    return problems


def main():
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "*", "golden.yaml")))
    if not paths:
        sys.exit("no golden.yaml files found")

    all_problems = []
    for path in paths:
        problems = check(path)
        label = os.path.relpath(path, os.path.dirname(HERE))
        if problems:
            print(f"FAIL  {label}")
            for p in problems:
                print(f"        {p}")
        else:
            n = len((yaml.safe_load(open(path)) or {}).get("cases") or [])
            print(f"ok    {label}  ({n} cases: schema valid, all queries unique)")
        all_problems.extend(problems)

    print()
    if all_problems:
        print(f"{len(all_problems)} problem(s) across {len(paths)} golden file(s)")
        sys.exit(1)
    print(f"{len(paths)} golden file(s) clean")


if __name__ == "__main__":
    main()
