"""Shared, table-independent query strategies for the benchmark harness.

Strategies map `(query_text, size, fields)` to a SearchIndex `searchQuery` DSL object.
Fields are loaded from each table's `fields.yaml`; equal-weight strategies remove boosts.
`frontend_default` and compiled `production_current` serve as controls. Verbose clause
objects are required by Synapse's JSON adapter.
"""
import re


def _unboosted(fields):
    """Drop the `^weight` from each field so a strategy matches all fields equally."""
    return [f.split("^", 1)[0] for f in fields]


# ---------------------------------------------------------------- pre-strategy routing
# buildQueryClause (SearchQueryUseQueryOptions.ts:146) does NOT send the configured clause
# unconditionally. It intercepts two kinds of query text first and routes BOTH to
# simple_query_string — keeping the portal's `fields` if it has any — because neither
# survives multi_match + fuzziness:
#
#   * a double-quoted phrase — multi_match cannot honour the phrase operator, and
#   * a Synapse entity id  — fuzziness AUTO matches neighbouring ids (syn12345 -> syn12344).
#
# This happens ahead of the queryStrategy switch, so it applies to a portal that has
# customized its search AND to one that has not: both `frontend_default` and a compiled
# `production_current` go through it. Ports of containsQuotedPhrase() and
# SYNAPSE_ENTITY_ID_TOKEN_REGEX (RegularExpressions.ts:37).
_QUOTED_PHRASE_RE = re.compile(r'"[^"]+"')
_SYNAPSE_ID_RE = re.compile(r"\bsyn\d+(?:\.\d+)?\b", re.IGNORECASE)


def routes_to_simple_query_string(q):
    """True when the frontend would bypass `queryStrategy` and emit simple_query_string."""
    return bool(_QUOTED_PHRASE_RE.search(q) or _SYNAPSE_ID_RE.search(q))


def frontend_default(q, size, fields):
    """Return the Synapse frontend's uncustomized query.

    Uses fuzzy `multi_match` across all fields, except quoted phrases and Synapse IDs use
    `simple_query_string`. It ignores `fields`. A platform-default benchmark also requires
    an index without a bound SearchConfiguration; `run.py` warns otherwise.
    """
    if routes_to_simple_query_string(q):
        return {"query": {"simple_query_string": {"query": q}}, "size": size}
    return {"query": {"multi_match": {"query": q, "fuzziness": "AUTO"}}, "size": size}


def simple_query_string(q, size, fields):
    return {"query": {"simple_query_string": {"query": q, "fields": _unboosted(fields)}}, "size": size}


def multi_match_best(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": _unboosted(fields), "type": "best_fields"}}, "size": size}


def multi_match_boosted(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "best_fields"}}, "size": size}


def multi_match_cross(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "cross_fields"}}, "size": size}


def boosted_fuzzy(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "best_fields", "fuzziness": "AUTO"}}, "size": size}


def phrase_prefix(q, size, fields):
    # typeahead-style: matches a leading prefix on the last token
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "phrase_prefix"}}, "size": size}


def simple_query_string_boosted(q, size, fields):
    # The clause the routing above produces for a portal that sets fieldBoosts: a
    # simple_query_string that KEEPS the boosts. `simple_query_string` (unboosted) is the
    # equal-weight variant; no other strategy here matches this shape.
    return {"query": {"simple_query_string": {"query": q, "fields": fields}}, "size": size}


# queryStrategy value -> the clause it emits, given the portal's boosted field list.
# Ports the switch in buildQueryClause. MULTI_MATCH (the platform default) is the one case
# that ignores fieldBoosts entirely, which is why frontend_default sends no field list.
_QUERY_STRATEGY_CLAUSE = {
    "MULTI_MATCH": lambda q, fields, fuzz: {
        "multi_match": {"query": q, "fuzziness": fuzz or "AUTO"}},
    "MULTI_MATCH_BEST_FIELDS": lambda q, fields, fuzz: {
        "multi_match": {"query": q, "type": "best_fields", "fields": fields,
                        **({"fuzziness": fuzz} if fuzz else {})}},
    "MULTI_MATCH_CROSS_FIELDS": lambda q, fields, fuzz: {
        "multi_match": {"query": q, "type": "cross_fields", "fields": fields,
                        **({"fuzziness": fuzz} if fuzz else {})}},
    "SIMPLE_QUERY_STRING": lambda q, fields, fuzz: {
        "simple_query_string": {"query": q, "fields": fields}},
    "PHRASE_PREFIX": lambda q, fields, fuzz: {
        "multi_match": {"query": q, "type": "phrase_prefix", "fields": fields}},
    "BOOSTED_FUZZY": lambda q, fields, fuzz: {
        "multi_match": {"query": q, "fields": fields, "fuzziness": fuzz or "AUTO"}},
}


def compile_production(spec):
    """Compile a table's `production:` specification into a strategy.

    The returned strategy uses the configured query strategy, fields, and fuzziness rather
    than benchmark fields, and applies frontend phrase and Synapse-ID routing.
    """
    strategy = spec.get("query_strategy", "MULTI_MATCH")
    if strategy not in _QUERY_STRATEGY_CLAUSE:
        raise ValueError(f"unknown query_strategy {strategy!r}; "
                         f"expected one of {sorted(_QUERY_STRATEGY_CLAUSE)}")
    prod_fields = spec.get("fields") or None
    fuzziness = spec.get("fuzziness")
    clause = _QUERY_STRATEGY_CLAUSE[strategy]

    def production_current(q, size, fields):
        # Routing happens before the queryStrategy switch and does not depend on
        # fieldBoosts — a portal with no boosts still gets a bare simple_query_string.
        if routes_to_simple_query_string(q):
            sqs = {"query": q}
            if prod_fields:
                sqs["fields"] = prod_fields
            return {"query": {"simple_query_string": sqs}, "size": size}
        return {"query": clause(q, prod_fields, fuzziness), "size": size}

    return production_current


STRATEGIES = {
    "frontend_default": frontend_default,
    "simple_query_string": simple_query_string,
    "simple_query_string_boosted": simple_query_string_boosted,
    "multi_match_best": multi_match_best,
    "multi_match_boosted": multi_match_boosted,
    "multi_match_cross": multi_match_cross,
    "boosted_fuzzy": boosted_fuzzy,
    "phrase_prefix": phrase_prefix,
}
