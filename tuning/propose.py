"""The agent step: ask Claude to propose candidate search configs.

Given the index profile, the golden set summary, the current leaderboard, and per-case
relevance diagnostics, Claude proposes a batch of new candidates — different query types,
field selections, and starting boosts — each with a one-line rationale. This is the
"suggest boosting and query types" half of the loop; the numeric optimizer then tunes the
boosts of whatever structures come back.

Structured output is obtained via **forced tool use**: a single `propose_candidates` tool
whose `input_schema` is the candidate-batch shape, with `tool_choice` pinned to it. Claude's
tool_use `input` is already a parsed dict matching the schema — no text parsing, and it works
across SDK versions (no dependency on `messages.parse` / `output_config`). Stable context
(profile, golden summary, allowed fields, the schema rules) lives in a cached system prompt so
each round only pays for the changing leaderboard + diagnostics. Model defaults to
claude-opus-4-8. Credentials resolve the usual way (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
`ant auth login`)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from candidate import MM_TYPES, QUERY_TYPES        # noqa: E402

DEFAULT_MODEL = "claude-opus-4-8"

# JSON Schema for the forced tool. Optional fields accept null so the model can omit them.
_CANDIDATE_SCHEMA = {
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
_TOOL = {
    "name": "propose_candidates",
    "description": "Return the batch of candidate search configurations to try.",
    "input_schema": {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": _CANDIDATE_SCHEMA}},
        "required": ["candidates"],
    },
}


def _to_candidate(c: dict) -> dict:
    """Raw tool-input candidate -> the plain candidate dict build_dsl/normalize expect."""
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


SYSTEM_RULES = """You are a search-relevance tuning expert for an OpenSearch index. Your job: \
propose candidate query configurations ("candidates") that should rank the known-correct \
results higher on a golden relevance benchmark.

A candidate is a query-time recipe (no index/analyzer changes):
- query_type: "multi_match" (recommended) or "simple_query_string".
- multi_match_type: best_fields (max over fields — good when one field should win),
  most_fields (sum — rewards matching in several fields), cross_fields (treats fields as one
  big field — good for names split across columns), phrase / phrase_prefix (order-sensitive).
- fields: which columns to search and each one's boost (>1 = more important). Pick from the
  ALLOWED FIELDS only; you may use a subset.
- fuzziness: "AUTO" tolerates typos (best_fields/most_fields/cross_fields only).
- tie_breaker (best_fields only, 0–1): how much non-winning fields still contribute.
- minimum_should_match: e.g. "2<75%" to require more term overlap on longer queries.
- phrase_boost: optionally bump exact-phrase matches in some fields without requiring them.

Use the profile (what the data is about), the golden summary, the leaderboard (what already
works), and the per-case diagnostics (which fields the ideal docs match or miss, and what \
outranks them) to reason about WHY cases fail, then propose fixes. Vary the structures you \
return — different query types, field sets, and boost shapes — don't return near-duplicates. \
A numeric optimizer will fine-tune the exact boost numbers afterward, so focus on the \
structure and sensible starting boosts. Call the propose_candidates tool with your batch."""


def _profile_brief(profile):
    """Compact the profile for the prompt (roles + category vocab are the useful parts)."""
    return {
        "total_hits": profile.get("total_hits"),
        "roles": profile.get("roles"),
        "columns": [{"name": c["name"], "role": c["role"], "fill": c["fill"],
                     "samples": c["samples"]} for c in profile.get("columns", [])],
    }


def propose(profile, golden_summary, allowed_fields, current_boosts, leaderboard,
            diagnostics, n=6, model=DEFAULT_MODEL):
    """Return a list of proposed candidate dicts (un-normalized; tune.py normalizes/validates).
    Raises if the Anthropic SDK / credentials are unavailable — callers gate on --no-agent."""
    import anthropic  # imported here so --no-agent needs neither the package nor a key

    client = anthropic.Anthropic()
    stable = (
        SYSTEM_RULES
        + "\n\nINDEX PROFILE:\n" + json.dumps(_profile_brief(profile), indent=2)
        + "\n\nGOLDEN SET:\n" + json.dumps(golden_summary, indent=2)
        + "\n\nALLOWED FIELDS (use only these):\n" + json.dumps(allowed_fields)
        + "\n\nCURRENT DEFAULT BOOSTS (the existing fields.yaml):\n" + json.dumps(current_boosts)
    )
    user = (
        f"Propose {n} candidate configurations.\n\n"
        "CURRENT LEADERBOARD (best configs so far, by the objective metric):\n"
        + json.dumps(leaderboard, indent=2)
        + "\n\nPER-CASE DIAGNOSTICS (hardest cases first):\n"
        + json.dumps(diagnostics, indent=2)
    )
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=[{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "propose_candidates"},
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_candidates":
            return [_to_candidate(c) for c in block.input.get("candidates", [])]
    return []
