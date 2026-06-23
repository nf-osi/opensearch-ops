"""Shared query strategies for the benchmark harness (all tables).

A strategy is a function mapping (query_text, size, fields) -> a raw OpenSearch
`searchQuery` DSL object (the value of SearchIndexQuery.searchQuery). The strategy
*functions* are table-agnostic; `fields` is the table's match list with per-field boosts
(`["resourceName^5", "synonyms^4", ...]`), loaded by run.py from
`benchmark/<table>/fields.yaml` (falling back to ["*"] — all fields, no boosts — when a
table has none). The equal-weight strategies strip the `^weight` themselves via
`_unboosted()`; the boosted strategies pass `fields` through as-is.

All clauses use the verbose object form because Synapse's JSON adapter rejects shorthand.
"""


def _unboosted(fields):
    """Drop the `^weight` from each field so a strategy matches all fields equally."""
    return [f.split("^", 1)[0] for f in fields]


def frontend_default(q, size, fields):
    # EXACT shape the live Synapse frontend sends today (synapse-web-monorepo,
    # SearchQueryUseQueryOptions.ts; see docs/INTEGRATION.md): a bare multi_match with
    # fuzziness AUTO — NO field list (searches all fields, equal weight), no boosts, no
    # explicit type (best_fields default). The production baseline every other strategy is
    # measured against; intentionally ignores `fields`.
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


STRATEGIES = {
    "frontend_default": frontend_default,
    "simple_query_string": simple_query_string,
    "multi_match_best": multi_match_best,
    "multi_match_boosted": multi_match_boosted,
    "multi_match_cross": multi_match_cross,
    "boosted_fuzzy": boosted_fuzzy,
    "phrase_prefix": phrase_prefix,
}
