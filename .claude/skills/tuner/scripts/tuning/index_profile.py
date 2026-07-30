"""Infer a SearchIndex's column roles from a sample of its documents.

The harness needs this for exactly two things, both when a table has no hand-authored
`fields.yaml` to start from:

  - `candidate.bootstrap_boosts()` — turn the inferred roles into a starting {field: boost}
    map (title highest, then other names, categories, prose, boilerplate; identifiers and
    non-text columns excluded). See `candidate.ROLE_BOOSTS` for the weights.
  - `probe.profile_brief()` — the compacted profile that goes into `round_context.json` for
    the proposing agent to read.

Mostly schema-agnostic — it works across portals off value shape and the index's own column
types. The single exception is `_TITLE_SUFFIXES`, a name-based prior for the title column,
because no value statistic separates a title from a description. The sample comes from one
`match_all` query and is **order-biased** — treat fill rates and category counts as a feel for
the data, not ground truth.

This mirrors the role heuristic in the `goldie` skill's `profile_index.py` (same
thresholds, same output shape, so the two stay comparable), minus its CLI. It is duplicated
rather than imported on purpose: this skill stays self-contained, with no cross-skill script
loading and no path discovery. goldie's version remains the richer one — it also profiles
the *source table*, which is the actual oracle; use it directly when you need that.
"""
import collections
import json
import re

from client import KEYWORD_TYPES, hit_dict, is_text_searchable, search


def values_of(v):
    """Normalize a field value into a list of scalar strings (decodes JSON arrays)."""
    if v is None:
        return []
    s = v if isinstance(v, str) else str(v)
    s = s.strip()
    if s in ("", "[]"):
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            pass
    return [s]


def trunc(s, n=60):
    s = str(s).replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


# Columns whose *name* marks them as the record's title. A deliberate exception to this
# module's otherwise schema-agnostic stance: every hand-curated fields.yaml in benchmark/ puts
# the title column in the top boost tier (`resourceName^5`, `publicationTitle^5`), and no
# value-shape statistic separates a title from a description — nf-studies' `studyName` averages
# 81 chars and nf-tools' `description` averages 89. Getting this wrong is expensive: `studyName`
# and `name` used to land in the BOTTOM tier while dates sat at the top, and a whole tuning run
# was spent rediscovering that. Treated as a prior only — the optimizer retunes every boost.
_TITLE_SUFFIXES = ("name", "title", "label")


def _is_title(col):
    c = (col or "").lower()
    return c in _TITLE_SUFFIXES or any(c.endswith(sfx) for sfx in _TITLE_SUFFIXES)


# Fraction of sampled values that are distinct, above which a long free-text column counts as
# substantive prose rather than repeated boilerplate. nf-studies: summary 0.99 vs
# accessRequirements 0.14 and acknowledgementStatements 0.36 (the same legal paragraph on every
# row); nf-tools: description 0.71 vs howToAcquire 0.12. The gap is wide, so the exact cut is
# not delicate.
_SUBSTANTIVE_UNIQ = 0.5


def _role(s, ctype=None, col=None):
    """Infer a column's role from its sample stats (heuristic — verify before relying).

    `ctype` is the column's Synapse type from the index's own schema, and it OVERRIDES the
    value heuristics where the two disagree, because the heuristics work on stringified values
    and cannot see a type:

      - a non-text type (DATE, INTEGER, …) gets no role at all, so it never reaches a boost
        map. Dates are the motivating case: stored as epoch millis, `"1496188800000"` is 13
        chars and all-distinct, which sails through the `name?` test below and lands in the
        TOP boost tier — and then every query 500s with number_format_exception.
      - a keyword type (ENTITYID, …) is an identifier by construction. The `id?` test alone
        misses the multi-valued ones (`relatedStudies`), because a list column repeats values
        across rows and so fails the ~all-distinct check.
    """
    if ctype is not None:
        if not is_text_searchable(ctype):
            return ""
        if ctype in KEYWORD_TYPES:
            return "id?"
    nd = len(s["distinct"])
    uniq = (nd / s["n_vals"]) if s["n_vals"] else 0.0
    # A title beats the value-shape rules below, but only when it looks like real content:
    # mostly-filled and not one repeated string (which would be a constant, not a title).
    if col is not None and _is_title(col) and s["fill"] >= 0.5 and nd > 1:
        return "title"
    if s["fill"] >= 0.9 and s["n_vals"] and nd >= 0.95 * s["n_vals"] and s["avg_len"] <= 64:
        return "id?"          # high fill, ~all distinct → identifier candidate
    if 1 < nd <= 12 and s["avg_len"] <= 40:
        return "category"     # low cardinality, short values → topical/facet candidate
    if s["avg_len"] > 80:
        # Long values split by variety: real prose worth searching, vs the same paragraph
        # repeated on every row (access terms, acknowledgements, how-to-acquire). Boilerplate
        # matches many docs on shared wording, so weighting it up mostly adds distractors —
        # the optimizer once pushed accessRequirements to the boost ceiling on 31 cases.
        return "text" if uniq >= _SUBSTANTIVE_UNIQ else "boilerplate"
    if s["fill"] >= 0.5 and s["avg_len"] <= 64 and nd > 12:
        return "name?"        # high-variety short strings → name/title candidate
    return ""


# --- analyzer reachability -----------------------------------------------------------------
#
# A column's Synapse TYPE says which query shapes won't 500 (see client.py). It says nothing
# about which ANALYZER the index's SearchConfiguration routed the column through, and that
# decides whether a real user query can match it at all. Columns mapped to KEYWORD match their
# whole value exactly and CASE-SENSITIVELY: on nf-studies, `fundingAgency:NTAP` returns 101 hits
# and `fundingAgency:ntap` returns 0, though both are STRING_LIST and both look perfectly
# searchable to every other check in this harness.
#
# This is worth measuring rather than assuming, because it is invisible everywhere else: the
# field probe scores such a column 0.0 on every lowercase case, which reads as "this field is
# uninformative" rather than "this field is unreachable", and the optimizer duly spends its
# sweep weighting a column that can never contribute.

# A probe value needs an uppercase character (else lowercasing it is a no-op and the test says
# nothing) and should be short: a KEYWORD column matches the WHOLE value, so a paragraph is both
# unlikely to round-trip and expensive to send.
_CASE_PROBE_MAXLEN = 80


def _case_probe_value(values):
    """The shortest sampled value carrying an uppercase letter, or None if none qualifies."""
    usable = [v for v in values
              if v and len(v) <= _CASE_PROBE_MAXLEN and v != v.lower()]
    return min(usable, key=len) if usable else None


def _case_probe_token(values):
    """Fallback for prose columns, whose values all blow the length cap: a single uppercase-
    bearing word drawn from them.

    Weaker but sound in the one direction that matters. A token can only match if the column is
    tokenized, so on a KEYWORD column (whole-value match) the stored-casing query returns 0 and
    the caller reports "unknown" — never a false "analyzed". Acronyms are the common case here
    and exactly what users type: without this, `summary` on nf-studies goes unclassified purely
    for being long."""
    for v in values:
        for tok in re.split(r"[^A-Za-z0-9]+", v or ""):
            if 3 <= len(tok) <= 30 and tok != tok.lower() and not tok.isdigit():
                return tok
    return None


def _field_hits(index, field, text, poll_s):
    res = search(index, {"query": {"multi_match": {"query": text, "fields": [field]}}, "size": 1},
                 response_parts=["TOTAL_HITS"], poll_s=poll_s)
    return res.get("totalHits") or 0


def analyzer_reachability(index, fields, n=100, poll_s=0.2):
    """Which of `fields` a LOWERCASE free-text query can actually match.

    For each field, take a sampled value containing an uppercase letter and query that field for
    it twice — once as stored, once lowercased:

      "analyzed"        stored hits > 0 and lowercased hits > 0 — a normal lowercasing analyzer;
                        the field behaves the way the rest of the harness assumes.
      "case_sensitive"  stored hits > 0 but lowercased hits == 0 — KEYWORD-mapped. Reachable
                        only by a query that reproduces the stored casing exactly.
      "unknown"         no sampled value carried an uppercase letter, or the stored value didn't
                        match its own field. Not a verdict — callers must leave these alone.

    Returns {field: verdict}. Costs one sampling query plus two per testable field.

    Note what "case_sensitive" does and doesn't license. Such a field is dead weight for
    lowercase queries but still matches a user who types the stored casing (`Synodos NF2`,
    an RRID), so callers should FLAG it, not drop it — unlike a non-text column, which the
    index rejects outright. Whether it's dead for a given run depends on that run's golden
    queries; see tune.py's `_reachability_note`.
    """
    res = search(index, {"query": {"match_all": {}}, "size": n},
                 response_parts=["HITS"], poll_s=poll_s)
    docs = [hit_dict(h) for h in res.get("hits", [])]
    out = {}
    for f in fields:
        vals = []
        for d in docs:
            vals.extend(values_of(d.get(f)))
        uniq = list(dict.fromkeys(vals))
        probe_val = _case_probe_value(uniq) or _case_probe_token(uniq)
        if probe_val is None:
            out[f] = "unknown"
            continue
        try:
            stored = _field_hits(index, f, probe_val, poll_s)
            if not stored:
                out[f] = "unknown"          # value didn't match its own field — don't guess
                continue
            lowered = _field_hits(index, f, probe_val.lower(), poll_s)
        except Exception:
            out[f] = "unknown"              # a probe failure is not evidence of anything
            continue
        out[f] = "analyzed" if lowered else "case_sensitive"
    return out


def profile(index, n=100, poll_s=0.2):
    """Profile a SearchIndex from a <=n-row match_all sample. Returns:

      {index, total_hits, sampled, types: {col: columnType},
       columns: [{name, role, type, fill, n_distinct, samples:[...], avg_len}],
       roles: {identifier:[...], title:[...], name:[...], text:[...], boilerplate:[...],
               category: {col: {value: count, ...}},
               phrase_unsafe: [...]}}

    `types` comes from the index's own `selectColumns` in the same query (free) and gates the
    role heuristics — see _role(). `roles.phrase_unsafe` lists searchable-but-keyword columns,
    which a phrase / phrase_prefix query must not touch.

    The API caps returned hits at 100 regardless of `n`."""
    res = search(index, {"query": {"match_all": {}}, "size": n},
                 response_parts=["HITS", "TOTAL_HITS", "SELECT_COLUMNS"], poll_s=poll_s)
    types = {c["name"]: c.get("columnType") for c in (res.get("selectColumns") or [])}
    docs = [hit_dict(h) for h in res.get("hits", [])]
    if not docs:
        return {"index": index, "total_hits": res.get("totalHits"), "sampled": 0, "types": types,
                "columns": [], "roles": {"identifier": [], "title": [], "name": [],
                                         "text": [], "boilerplate": [], "category": {},
                                         "phrase_unsafe": []}}
    m = len(docs)
    cols = collections.OrderedDict((k, None) for d in docs for k in d)
    stats = {}
    for k in cols:
        vals, filled = [], 0
        for d in docs:
            vs = values_of(d.get(k))
            if vs:
                filled += 1
                vals.extend(vs)
        distinct = list(dict.fromkeys(vals))
        stats[k] = dict(fill=filled / m, n_vals=len(vals), distinct=distinct,
                        avg_len=(sum(len(x) for x in vals) / len(vals)) if vals else 0)

    roles = {k: _role(stats[k], types.get(k), col=k) for k in cols}
    columns = [{"name": k, "role": roles[k], "type": types.get(k),
                "fill": round(stats[k]["fill"], 3),
                "n_distinct": len(stats[k]["distinct"]), "avg_len": round(stats[k]["avg_len"], 1),
                "samples": [trunc(x) for x in stats[k]["distinct"][:5]]}
               for k in cols]
    cat_dists = {}
    for k in cols:
        if roles[k] == "category":
            dist = collections.Counter()
            for d in docs:
                for v in values_of(d.get(k)):
                    dist[v] += 1
            cat_dists[k] = dict(dist.most_common(12))
    return {
        "index": index, "total_hits": res.get("totalHits"), "sampled": m, "types": types,
        "columns": columns,
        "roles": {
            "identifier": [c["name"] for c in columns if c["role"] == "id?"],
            "title": [c["name"] for c in columns if c["role"] == "title"],
            "name": [c["name"] for c in columns if c["role"] == "name?"],
            "text": [c["name"] for c in columns if c["role"] == "text"],
            # long but low-variety prose (repeated legal/contact text) — searchable, but the
            # weakest signal on the table; kept separate so it isn't boosted like real prose
            "boilerplate": [c["name"] for c in columns if c["role"] == "boilerplate"],
            "category": cat_dists,
            # searchable, but a phrase/phrase_prefix query against these 500s (keyword mapping)
            "phrase_unsafe": [c["name"] for c in columns
                              if c["type"] in KEYWORD_TYPES],
        },
    }
