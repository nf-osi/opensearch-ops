"""Shared query strategies for the benchmark harness (all tables).

A strategy is a function mapping (query_text, size, fields) -> a raw OpenSearch
`searchQuery` DSL object (the value of SearchIndexQuery.searchQuery). The strategy
*functions* are table-agnostic; `fields` is the table's match list with per-field boosts
(`["resourceName^5", "synonyms^4", ...]`), loaded by run.py from
`benchmark/<table>/fields.yaml` (falling back to ["*"] — all fields, no boosts — when a
table has none). The equal-weight strategies strip the `^weight` themselves via
`_unboosted()`; the boosted strategies pass `fields` through as-is.

Two of the entries are controls rather than candidates: `frontend_default` is the platform
default a portal gets with no `SearchQueryConfig`, and `production_current` — compiled per
table by `compile_production()` from the `production:` block in fields.yaml — is what a
portal that HAS one actually sends. Which of the two is a given table's control is declared
by `control:` in its fields.yaml and recorded in the run JSON; see run.py.

All clauses use the verbose object form because Synapse's JSON adapter rejects shorthand.
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
    """The Synapse frontend's PLATFORM default — what a portal gets when it has not set a
    `SearchQueryConfig`: a bare multi_match with fuzziness AUTO, NO field list (all fields,
    equal weight), no boosts, no explicit type (best_fields default) — EXCEPT for quoted
    and Synapse-id queries, which route to a bare simple_query_string (see above).

    This is the control for the 12 NF indexes that ship no config. It is NOT the control
    for nf-tools, which does set one — see `compile_production` / the `production:` block
    in fields.yaml. Intentionally ignores `fields`.

    IMPORTANT — "no customization" includes the INDEX, not just the query. Scoring this
    against a SearchIndex with a SearchConfiguration bound measures the default query on a
    CUSTOMIZED index, which is a different and usually worse number. Index state is shared
    by every strategy in a run, so this cannot be corrected within one run. To get a true
    platform default, unbind first (`config.py unbind`), score, then re-bind, and graft
    that row into the published run via site.yaml. run.py records the bound config id on
    every run and warns when this strategy is scored against a bound index.
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
    """Compile a table's `production:` block (fields.yaml) into a strategy function.

    The block records what the portal actually sends for this table — its
    `SearchQueryConfig` (queryStrategy / fieldBoosts / fuzziness) transcribed from
    synapseConfigs. The compiled strategy pins those fields rather than the benchmark's
    own `fields` list, so it stays a faithful control even as fields.yaml is tuned, and it
    applies the quoted-phrase / Synapse-id routing above.

    A table with no `production:` block has no such customization; its control is
    `frontend_default` (which is exactly the MULTI_MATCH branch).
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
