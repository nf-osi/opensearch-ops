#!/usr/bin/env python3
"""Validate the golden relevance sets for duplicate queries (and duplicate case ids).

WHY duplicate queries matter: run.py sends `case["query"]` to the index and scores the
ranked ids against `case["relevant"]`. Two cases with the same query therefore issue the
SAME request twice — they cannot disagree about ranking, only about ground truth. That
double-counts one query in every aggregate (MRR / Recall@k / Hit@1 / Hit@k are plain means
over cases), so a duplicated query silently reweights the benchmark toward whatever that
query happens to measure. It is also the most likely way a golden drifts: the same user
question gets curated twice, months apart, with two different `relevant` sets, and nobody
notices because both cases "pass".

Queries are compared case-insensitively with runs of whitespace collapsed, because the
index lowercases at analysis time — `NTAP MRI` and `ntap mri` are the same request and
would score identically. Punctuation is deliberately NOT normalized: `mpnst rna-seq` and
`mpnst rna seq` analyze differently (word_delimiter), so they are genuinely distinct
queries and the golden is allowed to hold both. Likewise `ntap mri` vs `NTAP AND MRI` are
distinct on purpose (see the ntap-and-mri-operator case).

Duplicate case ids are checked too: run.py writes per_case results into a dict keyed by
`case["id"]`, so a repeated id makes one case's scores silently overwrite the other's.

Usage:
  python3 benchmark/validate_goldens.py                    # all benchmark/*/golden.yaml
  python3 benchmark/validate_goldens.py path/to/golden.yaml [...]

Exit status: 0 = clean, 1 = at least one problem found. Under GitHub Actions it also
emits ::error file=,line= annotations so failures land on the diff.
"""
import glob
import os
import re
import sys
from collections import defaultdict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


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


def check(path):
    """Return a list of human-readable problems found in one golden file."""
    problems = []
    try:
        doc = yaml.safe_load(open(path)) or {}
    except yaml.YAMLError as exc:
        return [f"{path}: not valid YAML: {exc}"]

    cases = doc.get("cases") or []
    if not cases:
        return [f"{path}: no `cases:` list found"]

    lines = case_lines(path)

    by_query = defaultdict(list)
    by_id = defaultdict(list)
    for pos, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            problems.append(f"{path}: case #{pos} is not a mapping")
            continue
        cid = case.get("id") or f"<case #{pos}, no id>"
        by_id[cid].append(cid)
        if "query" not in case:
            problems.append(f"{path}: case `{cid}` has no `query`")
            continue
        by_query[normalize(case["query"])].append((cid, case["query"]))

    for norm, group in sorted(by_query.items()):
        if len(group) < 2:
            continue
        shown = ", ".join(f"`{cid}` (query: {raw!r})" for cid, raw in group)
        problems.append(
            f"{path}: query {norm!r} is used by {len(group)} cases: {shown}. "
            f"Merge them into one case, or change one query so the two cases measure "
            f"different requests."
        )
        for cid, _ in group:
            others = ", ".join(o for o, _ in group if o != cid)
            for line in lines.get(cid) or [1]:
                annotate(path, line,
                         f"duplicate query {norm!r} — also used by {others}")

    for cid, group in sorted(by_id.items()):
        if len(group) > 1:
            problems.append(f"{path}: case id `{cid}` is used {len(group)} times; "
                            f"ids must be unique (run.py keys per-case results on them)")
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
        n = len(yaml.safe_load(open(path)).get("cases") or []) if not problems else None
        label = os.path.relpath(path, os.path.dirname(HERE))
        if problems:
            print(f"FAIL  {label}")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"ok    {label}  ({n} cases, all queries unique)")
        all_problems.extend(problems)

    print()
    if all_problems:
        print(f"{len(all_problems)} problem(s) across {len(paths)} golden file(s)")
        sys.exit(1)
    print(f"{len(paths)} golden file(s) clean")


if __name__ == "__main__":
    main()
