"""Search "candidate" config as data, plus a DSL compiler.

A *candidate* is a plain dict describing a query-time search recipe — the levers this
harness tunes. It compiles to a raw OpenSearch `searchQuery` DSL via `build_dsl()`, the same
shape `query.py`/`benchmark/run.py` send. Making the recipe data (not a Python function, as
in `benchmark/strategies.py`) is what lets an agent emit candidates as JSON and lets the
numeric optimizer sweep boost vectors.

Candidate schema (all keys optional except query_type/fields):

    {
      "name": "cand_3",                       # label for the leaderboard
      "query_type": "multi_match",            # "multi_match" | "simple_query_string"
      "multi_match_type": "best_fields",      # best_fields|most_fields|cross_fields|phrase|phrase_prefix
      "fields": {"resourceName": 5, "synonyms": 4, "description": 1},  # {field: boost}; {} or None = all fields
      "fuzziness": "AUTO",                    # "AUTO" | 0 | 1 | 2 | None  (ignored for phrase types / sqs)
      "tie_breaker": 0.3,                     # float | None  (best_fields only)
      "minimum_should_match": "2<75%",        # str | None
      "phrase_boost": {"fields": ["resourceName", "synonyms"], "boost": 3}  # None, or a should-clause phrase bump
    }

Only the verbose *query-clause* object form is used where Synapse's JSON adapter requires it;
`field^boost` shorthand in a multi_match `fields` list IS accepted (the live frontend and
`benchmark/strategies.py` already rely on it)."""

MM_TYPES = ("best_fields", "most_fields", "cross_fields", "phrase", "phrase_prefix")
QUERY_TYPES = ("multi_match", "simple_query_string")
# query types whose combined score decomposes into per-field contributions, so the field
# probe can re-rank boost changes locally without new live queries (see probe.py).
DECOMPOSABLE = ("best_fields", "most_fields")


def _fmt_boost(b):
    """Render a boost as the `^N` suffix, omitting it for weight 1 and trimming trailing zeros."""
    try:
        b = float(b)
    except (TypeError, ValueError):
        return ""
    if abs(b - 1.0) < 1e-9:
        return ""
    s = f"{b:.3f}".rstrip("0").rstrip(".")
    return f"^{s}"


def field_list(fields):
    """{field: boost} -> ["field^boost", ...] (sorted by descending boost for readability)."""
    if not fields:
        return []
    items = sorted(fields.items(), key=lambda kv: (-float(kv[1]), kv[0]))
    return [f"{name}{_fmt_boost(b)}" for name, b in items]


def from_fields_yaml(fields):
    """Parse a benchmark fields.yaml list (["resourceName^5", "description", ...]) into
    {field: boost}. Bare "*" (all fields) yields {} (meaning: search all fields, no boosts)."""
    out = {}
    for f in fields or []:
        if f == "*":
            continue
        name, _, b = f.partition("^")
        out[name] = float(b) if b else 1.0
    return out


def _core_clause(candidate, query):
    """The primary query clause (multi_match or simple_query_string)."""
    qtype = candidate.get("query_type", "multi_match")
    fl = field_list(candidate.get("fields"))
    if qtype == "simple_query_string":
        body = {"query": query}
        if fl:
            body["fields"] = fl
        msm = candidate.get("minimum_should_match")
        if msm:
            body["minimum_should_match"] = msm
        return {"simple_query_string": body}

    # multi_match
    mm_type = candidate.get("multi_match_type", "best_fields")
    body = {"query": query, "type": mm_type}
    if fl:
        body["fields"] = fl
    # fuzziness/tie_breaker are meaningless (or rejected) for phrase types
    if mm_type not in ("phrase", "phrase_prefix"):
        fz = candidate.get("fuzziness")
        if fz is not None:
            body["fuzziness"] = fz
        tb = candidate.get("tie_breaker")
        if tb is not None:
            body["tie_breaker"] = tb
    msm = candidate.get("minimum_should_match")
    if msm:
        body["minimum_should_match"] = msm
    return {"multi_match": body}


def build_dsl(candidate, query, size):
    """Compile a candidate + user query into a raw OpenSearch searchQuery DSL object."""
    core = _core_clause(candidate, query)
    pb = candidate.get("phrase_boost")
    if pb and pb.get("fields"):
        # Keep the core clause as the matcher (must) and add a phrase clause as a ranking bump.
        phrase = {"multi_match": {"query": query, "type": "phrase",
                                  "fields": field_list({f: 1 for f in pb["fields"]}),
                                  "boost": float(pb.get("boost", 2))}}
        return {"query": {"bool": {"must": [core], "should": [phrase]}}, "size": size}
    return {"query": core, "size": size}


def normalize(candidate, allowed_fields, max_boost=10.0):
    """Coerce a (possibly agent-proposed) candidate into a valid, safe dict:
    clamp to known query/multi_match types, drop fields not in `allowed_fields`, clamp boosts.
    Returns a fresh dict; raises ValueError if nothing usable remains."""
    c = dict(candidate)
    c["query_type"] = c.get("query_type") if c.get("query_type") in QUERY_TYPES else "multi_match"
    if c["query_type"] == "multi_match":
        if c.get("multi_match_type") not in MM_TYPES:
            c["multi_match_type"] = "best_fields"
    allowed = set(allowed_fields)
    fields = {}
    for name, b in (c.get("fields") or {}).items():
        if name in allowed:
            try:
                fields[name] = max(0.0, min(float(b), max_boost))
            except (TypeError, ValueError):
                continue
    # drop zero-weight fields (a field with boost 0 contributes nothing)
    fields = {k: v for k, v in fields.items() if v > 0}
    if not fields and not (c.get("fields") == {} or c.get("fields") is None):
        raise ValueError(f"candidate {c.get('name')!r} has no usable fields after filtering")
    c["fields"] = fields
    pb = c.get("phrase_boost")
    if pb:
        pf = [f for f in (pb.get("fields") or []) if f in allowed]
        c["phrase_boost"] = {"fields": pf, "boost": max(0.0, min(float(pb.get("boost", 2)), max_boost))} if pf else None
    fz = c.get("fuzziness")
    if fz not in ("AUTO", 0, 1, 2, None):
        c["fuzziness"] = None
    return c


def seed_candidates(fields):
    """The current hand-authored strategies as preset candidates, built from this table's
    {field: boost} map (mirrors benchmark/strategies.py). `frontend_default` intentionally
    ignores `fields` (bare multi_match over all fields, fuzziness AUTO — the production
    baseline). The equal-weight presets flatten boosts to 1."""
    flat = {k: 1.0 for k in fields}
    return [
        {"name": "frontend_default", "query_type": "multi_match", "multi_match_type": "best_fields",
         "fields": None, "fuzziness": "AUTO"},
        {"name": "simple_query_string", "query_type": "simple_query_string", "fields": flat},
        {"name": "multi_match_best", "query_type": "multi_match", "multi_match_type": "best_fields",
         "fields": flat},
        {"name": "multi_match_boosted", "query_type": "multi_match", "multi_match_type": "best_fields",
         "fields": dict(fields)},
        {"name": "multi_match_cross", "query_type": "multi_match", "multi_match_type": "cross_fields",
         "fields": dict(fields)},
        {"name": "boosted_fuzzy", "query_type": "multi_match", "multi_match_type": "best_fields",
         "fields": dict(fields), "fuzziness": "AUTO"},
        {"name": "phrase_prefix", "query_type": "multi_match", "multi_match_type": "phrase_prefix",
         "fields": dict(fields)},
    ]


def to_fields_yaml(fields):
    """Render a {field: boost} map as fields.yaml `fields:` list entries, highest boost first."""
    return field_list(fields)
