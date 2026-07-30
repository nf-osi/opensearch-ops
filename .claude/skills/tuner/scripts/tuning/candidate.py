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
    # fuzziness is rejected outright by the live index for phrase types AND cross_fields
    # (confirmed live: "Fuzziness not allowed for type [cross_fields]") — despite what older
    # comments here claimed; tie_breaker is best_fields-only and harmless elsewhere, but we
    # only bother setting it where it does something.
    if mm_type not in ("phrase", "phrase_prefix", "cross_fields"):
        fz = candidate.get("fuzziness")
        if fz is not None:
            body["fuzziness"] = fz
    if mm_type == "best_fields":
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


PHRASE_TYPES = ("phrase", "phrase_prefix")


def normalize(candidate, allowed_fields, max_boost=10.0, phrase_unsafe=()):
    """Coerce a (possibly agent-proposed) candidate into a valid, safe dict:
    clamp to known query/multi_match types, drop fields not in `allowed_fields`, clamp boosts.
    Returns a fresh dict; raises ValueError if nothing usable remains.

    `phrase_unsafe` is the set of keyword-mapped columns (see client.KEYWORD_TYPES). They are
    dropped from phrase / phrase_prefix candidates and from `phrase_boost`, because the index
    rejects those query shapes on a keyword field with a 500 rather than just ranking it badly.
    Dropping the field is the right call over dropping the candidate: the phrase intent is
    still expressible over the analyzed-text fields that remain."""
    c = dict(candidate)
    c["query_type"] = c.get("query_type") if c.get("query_type") in QUERY_TYPES else "multi_match"
    if c["query_type"] == "multi_match":
        if c.get("multi_match_type") not in MM_TYPES:
            c["multi_match_type"] = "best_fields"
    unsafe = set(phrase_unsafe)
    allowed = set(allowed_fields)
    if unsafe and c["query_type"] == "multi_match" and c.get("multi_match_type") in PHRASE_TYPES:
        allowed -= unsafe
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


def from_query_block(spec, fields, name="deployed_config"):
    """A fields.yaml `query:` block + its `fields:` boosts -> a candidate dict.

    The inverse of `query_block()`, so a config this harness previously recommended and someone
    then deployed comes back in as something it can score. Without it, re-tuning an
    already-tuned table silently measures the winner against generic seeds only, and the one
    number that matters — did we beat what is actually running? — is missing from the
    leaderboard."""
    c = {"name": name, "query_type": spec.get("query_type", "multi_match"),
         "fields": dict(fields)}
    if c["query_type"] == "multi_match":
        c["multi_match_type"] = spec.get("multi_match_type") or "best_fields"
    for key in ("fuzziness", "tie_breaker", "minimum_should_match"):
        if spec.get(key) is not None:
            c[key] = spec[key]
    pb = spec.get("phrase_boost")
    if pb and pb.get("fields"):
        c["phrase_boost"] = {"fields": list(pb["fields"]), "boost": float(pb.get("boost", 2))}
    return c


def seed_candidates(fields, phrase_unsafe=(), deployed=None):
    """The current hand-authored strategies as preset candidates, built from this table's
    {field: boost} map (mirrors benchmark/strategies.py). `frontend_default` intentionally
    ignores `fields` (bare multi_match over all fields, fuzziness AUTO — the production
    baseline). The equal-weight presets flatten boosts to 1.

    `deployed` is the table's fields.yaml `query:` block, if it has one. It becomes an extra
    `deployed_config` seed so the leaderboard shows what is actually running today, not just
    the generic shapes — the comparison a maintainer deciding whether to re-deploy needs.

    `phrase_unsafe` (keyword-mapped columns) is excluded from the phrase_prefix seed, which
    the index would otherwise reject with a 500. That seed is dropped entirely if no
    analyzed-text field survives — better a leaderboard missing one preset than an `init` that
    dies partway through after minutes of live queries. IMPORTANT: seed 0 must stay
    `frontend_default` (see `frontend_default_seed`)."""
    flat = {k: 1.0 for k in fields}
    phrase_fields = {k: v for k, v in fields.items() if k not in set(phrase_unsafe)}
    seeds = [
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
    ]
    if phrase_fields:
        seeds.append({"name": "phrase_prefix", "query_type": "multi_match",
                      "multi_match_type": "phrase_prefix", "fields": phrase_fields})
    if deployed:
        seeds.append(from_query_block(deployed, fields))
    return seeds


DEPLOYED_SEED_NAME = "deployed_config"


def frontend_default_seed(fields):
    """The `frontend_default` preset on its own — the production baseline every comparison is
    made against. A named accessor rather than `seed_candidates(...)[0]`, so adding or
    reordering seeds can't silently repoint the baseline at some other query."""
    for s in seed_candidates(fields):
        if s["name"] == "frontend_default":
            return s
    raise AssertionError("frontend_default seed missing")


# Starting boost per inferred role, calibrated against the hand-curated fields.yaml files in
# benchmark/ rather than invented:
#
#   benchmark/tools/fields.yaml ......... resourceName^5, synonyms^4/rrid^4,
#                                         manifestation/species/type^2-3, description (1)
#   benchmark/usage-publications/... .... publicationTitle^5, abstract^2, authors^2,
#                                         journal (1), citation (1)
#
# So: the title dominates, secondary names/synonyms sit just under it, categories mid, prose
# low-but-not-bottom, and repeated boilerplate at the floor. Identifiers are excluded outright —
# they are not useful free-text match targets.
#
# `text` at 2.0 (not 1.0) follows `abstract^2`: on tables whose topical signal lives in prose,
# the description IS where most matches are. The previous flat 1.0 for all text put nf-studies'
# `studyName` and `summary` in the BOTTOM tier beneath date columns, and the winning config that
# run eventually found had moved them up — so this is the correction that experiment argued for.
ROLE_BOOSTS = (
    ("title", 5.0),        # the record's name/title — strongest single signal
    ("name", 3.0),         # other short high-variety strings (synonyms, accessions, leads)
    ("category", 2.0),     # controlled vocab: the topical/discovery seeds
    ("text", 2.0),         # substantive prose (summary / abstract / description)
    ("boilerplate", 1.0),  # long but repeated across rows: access terms, acknowledgements
)


def bootstrap_boosts(profile):
    """Derive a starting {field: boost} map from an `index_profile.profile()` result, for
    tables with no existing fields.yaml (no hand-authored search strategy to start from).
    Weights follow ROLE_BOOSTS above; identifiers and non-text columns are excluded.

    This is only a starting point — the numeric optimizer and any agent proposals retune every
    boost from here — but it is not arbitrary: a bad starting order costs a round or more of
    tuning to undo, and the optimizer works on ratios, so burying the title under a date column
    is a real handicap rather than a cosmetic one."""
    roles = profile.get("roles", {})
    boosts = {}
    for role, weight in ROLE_BOOSTS:
        # `category` is a {col: {value: count}} map; the rest are plain lists.
        for f in roles.get(role) or ():
            boosts.setdefault(f, weight)
    return boosts


def to_fields_yaml(fields):
    """Render a {field: boost} map as fields.yaml `fields:` list entries, highest boost first."""
    return field_list(fields)


def query_block(candidate):
    """The `query:` block of a tuned fields.yaml: the candidate's query SHAPE, without boosts.

    Boosts stay in the `fields:` list (one field list per table, not two that can disagree),
    so this carries only what that list can't express — query type and the knobs. Keys are
    this module's own candidate-schema names, so benchmark/strategies.py's `tuned_strategy()`
    feeds the block straight back into `build_dsl()` with no translation step: the query the
    benchmark sends is compiled by the same function that scored the winner.

    Unset knobs are omitted rather than emitted as nulls, so a plain config stays a two-line
    block and only a genuinely fancy winner looks fancy."""
    spec = {"query_type": candidate.get("query_type", "multi_match")}
    if spec["query_type"] == "multi_match":
        spec["multi_match_type"] = candidate.get("multi_match_type") or "best_fields"
    for key in ("fuzziness", "tie_breaker", "minimum_should_match"):
        v = candidate.get(key)
        if v is not None:
            spec[key] = v
    pb = candidate.get("phrase_boost")
    if pb and pb.get("fields"):
        spec["phrase_boost"] = {"fields": list(pb["fields"]),
                                "boost": float(pb.get("boost", 2))}
    return spec


# candidate shape -> the FIXED benchmark/strategies.py strategy that sends the same query.
#
# This is now informational only. Every winner is reproducible via `run.py --strategy tuned`,
# which compiles the `query:` block that `query_block()` writes into tuned_fields.yaml — so a
# missing entry here no longer means "not reproducible", only "no single-purpose strategy
# happens to send this exact shape". Keyed by (query_type, multi_match_type, fuzziness is set);
# shapes using tie_breaker / minimum_should_match / phrase_boost have no fixed equivalent
# because those strategies take no knobs.
_STRATEGY_BY_SHAPE = {
    ("multi_match", "best_fields", True): "boosted_fuzzy",
    ("multi_match", "best_fields", False): "multi_match_boosted",
    ("multi_match", "most_fields", False): "multi_match_most_boosted",
    ("multi_match", "cross_fields", False): "multi_match_cross",
    ("multi_match", "phrase_prefix", False): "phrase_prefix",
    ("simple_query_string", None, False): "simple_query_string_boosted",
}


def equivalent_strategy(candidate):
    """The FIXED benchmark strategy sending the same query as `candidate`, or None if none does.

    None means only that no knob-free strategy matches this shape — the `tuned` strategy
    reproduces it either way. Callers use this to note "the plain `multi_match_boosted` sends
    this same query too", not to decide whether a winner is applicable."""
    if candidate.get("tie_breaker") is not None or candidate.get("minimum_should_match") \
            or candidate.get("phrase_boost"):
        return None
    qt = candidate.get("query_type", "multi_match")
    mm = candidate.get("multi_match_type", "best_fields") if qt == "multi_match" else None
    # frontend_default is the one shape with no field list — it ignores fields.yaml entirely.
    if qt == "multi_match" and not candidate.get("fields"):
        return "frontend_default" if candidate.get("fuzziness") is not None else None
    return _STRATEGY_BY_SHAPE.get((qt, mm, candidate.get("fuzziness") is not None))


# The JSON shape an agent should write into candidates.json for `tune.py add-candidates`
# (see sciops/agents/tuner/SKILL.md) — one object per candidate, `fields` as a list of
# {field, boost} pairs (JSON-tool-call friendly) rather than this module's internal
# {field: boost} dict shape. `to_candidate()` converts between the two.
AGENT_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "short descriptive label"},
        "rationale": {"type": "string", "description": "one line: why this should rank better"},
        "query_type": {"type": "string", "enum": list(QUERY_TYPES)},
        "multi_match_type": {"type": ["string", "null"], "enum": list(MM_TYPES) + [None]},
        "fields": {
            "type": "array",
            "description": "columns to search, with boosts (>1 = more important)",
            "items": {"type": "object",
                      "properties": {"field": {"type": "string"},
                                     "boost": {"type": "number"}},
                      "required": ["field", "boost"]},
        },
        "fuzziness": {"type": ["string", "null"], "enum": ["AUTO", "0", "1", "2", None]},
        "tie_breaker": {"type": ["number", "null"]},
        "minimum_should_match": {"type": ["string", "null"]},
        "phrase_boost": {
            "type": ["object", "null"],
            "properties": {"fields": {"type": "array", "items": {"type": "string"}},
                           "boost": {"type": "number"}},
        },
    },
    "required": ["name", "rationale", "query_type", "fields"],
}


def to_candidate(c: dict) -> dict:
    """Agent-authored candidate (AGENT_CANDIDATE_SCHEMA shape) -> the plain candidate dict
    normalize()/build_dsl() expect. Pass the result to normalize() before using it — this
    only reshapes fields into a dict, it doesn't validate/clamp anything."""
    fz = c.get("fuzziness")
    if fz in ("0", "1", "2"):
        fz = int(fz)
    pb = c.get("phrase_boost")
    return {
        "name": c.get("name", "proposed"),
        "rationale": c.get("rationale", ""),
        "query_type": c.get("query_type", "multi_match"),
        "multi_match_type": c.get("multi_match_type"),
        "fields": {fb["field"]: fb["boost"] for fb in (c.get("fields") or []) if "field" in fb},
        "fuzziness": fz,
        "tie_breaker": c.get("tie_breaker"),
        "minimum_should_match": c.get("minimum_should_match"),
        "phrase_boost": ({"fields": pb.get("fields", []), "boost": pb.get("boost", 2)}
                         if pb else None),
    }
