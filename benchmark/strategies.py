"""Shared query strategies for the benchmark harness (all tables).

A strategy is a function mapping (query_text, size, fields) -> a raw OpenSearch
`searchQuery` DSL object (the value of SearchIndexQuery.searchQuery). The strategy
*functions* are table-agnostic; `fields` is the table's match list with per-field boosts
(`["resourceName^5", "synonyms^4", ...]`), loaded by run.py from
`benchmark/<table>/fields.yaml` (falling back to ["*"] — all fields, no boosts — when a
table has none). The equal-weight strategies strip the `^weight` themselves via
`_unboosted()`; the boosted strategies pass `fields` through as-is.

Naming convention: a bare name is the EQUAL-WEIGHT form (boosts stripped), and a
`_boosted` suffix honours the `^weight` in fields.yaml. These fixed strategies are the
hand-authored hypotheses the benchmark compares — one query shape each, no knobs.

THE `tuned` STRATEGY is different, and is how a tuner winner gets reproduced. The fixed
strategies above can only express query type + boosts, so a winner using `tie_breaker`,
`minimum_should_match`, or `phrase_boost` had no strategy that reproduced it: applying its
boosts and re-running scored a DIFFERENT query than the one recommended. Rather than add a
combinatorial strategy per knob, fields.yaml carries an optional `query:` block naming the
full recipe, and `tuned_strategy()` compiles it (see run.py's `load_field_config`).

All clauses use the verbose object form because Synapse's JSON adapter rejects shorthand.
"""
import os
import sys

# The DSL compiler is IMPORTED from the tuner skill rather than re-implemented here. The tuner
# picks its winner by scoring `candidate.build_dsl()` output against the live index, so
# compiling the same recipe through the same function is what makes `--strategy tuned`
# reproduce the tuned score instead of merely approximating it. A second copy of the compiler
# is precisely how the previous drift arose (fields.yaml silently dropped the knobs above), so
# there is deliberately one implementation. candidate.py is pure-stdlib and imports nothing
# from its own package, so importing it standalone is safe.
_TUNING = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".claude", "skills", "tuner", "scripts", "tuning")
if _TUNING not in sys.path:
    sys.path.insert(0, _TUNING)
from candidate import build_dsl, from_fields_yaml  # noqa: E402


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


def simple_query_string_boosted(q, size, fields):
    # Same parser as above but honouring fields.yaml's `^weight`. simple_query_string accepts
    # the `field^boost` shorthand in its own `fields` list, same as multi_match.
    return {"query": {"simple_query_string": {"query": q, "fields": fields}}, "size": size}


def multi_match_best(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": _unboosted(fields), "type": "best_fields"}}, "size": size}


def multi_match_boosted(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "best_fields"}}, "size": size}


def multi_match_most(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": _unboosted(fields), "type": "most_fields"}}, "size": size}


def multi_match_most_boosted(q, size, fields):
    # most_fields SUMS the per-field scores, so it rewards a document matching in several
    # fields — a different bet from best_fields' "one field should win".
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "most_fields"}}, "size": size}


def multi_match_cross(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "cross_fields"}}, "size": size}


def boosted_fuzzy(q, size, fields):
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "best_fields", "fuzziness": "AUTO"}}, "size": size}


def phrase_prefix(q, size, fields):
    # typeahead-style: matches a leading prefix on the last token
    return {"query": {"multi_match": {"query": q, "fields": fields, "type": "phrase_prefix"}}, "size": size}


def tuned_strategy(spec):
    """Build a strategy function from fields.yaml's optional `query:` block.

    `spec` is the block as-is — its keys are exactly the tuner's candidate schema
    (`query_type`, `multi_match_type`, `fuzziness`, `tie_breaker`, `minimum_should_match`,
    `phrase_boost`), so it needs no translation to compile. `fields` is deliberately NOT read
    from the block: the boosts stay in the `fields:` list where every other strategy reads
    them, so there is one field list per table rather than two that can disagree.

    Registered by run.py as the `tuned` strategy whenever a table's fields.yaml has a `query:`
    block; tables without one (the untuned default) are unaffected.
    """
    if not isinstance(spec, dict):
        raise TypeError(f"fields.yaml `query:` must be a mapping, got {type(spec).__name__}")

    def tuned(q, size, fields):
        return build_dsl(dict(spec, fields=from_fields_yaml(fields)), q, size)

    return tuned


STRATEGIES = {
    "frontend_default": frontend_default,
    "simple_query_string": simple_query_string,
    "simple_query_string_boosted": simple_query_string_boosted,
    "multi_match_best": multi_match_best,
    "multi_match_boosted": multi_match_boosted,
    "multi_match_most": multi_match_most,
    "multi_match_most_boosted": multi_match_most_boosted,
    "multi_match_cross": multi_match_cross,
    "boosted_fuzzy": boosted_fuzzy,
    "phrase_prefix": phrase_prefix,
}
