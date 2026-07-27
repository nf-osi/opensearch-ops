"""The agent step: ask Claude to propose candidate search configs — for LOCAL/human CLI use
only (`tune.py run`, with your own ANTHROPIC_API_KEY in your shell). The Managed Agent path
(`tune.py init` / `add-candidates` / `finalize`, driven by sciops/agents/tuner/) does NOT use this
module — there, the calling agent (already an LLM, under the platform's own credentials)
authors candidates.json directly using `candidate.AGENT_CANDIDATE_SCHEMA`, no nested API call
or credential needed. This module exists so a human with their own key can still run the full
loop in one shot from a terminal.

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
claude-opus-5. Credentials resolve the usual way (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
`ant auth login`)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from candidate import AGENT_CANDIDATE_SCHEMA                       # noqa: E402
from probe import profile_brief                                   # noqa: E402

DEFAULT_MODEL = "claude-opus-5"

_TOOL = {
    "name": "propose_candidates",
    "description": "Return the batch of candidate search configurations to try.",
    "input_schema": {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": AGENT_CANDIDATE_SCHEMA}},
        "required": ["candidates"],
    },
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
- fuzziness: "AUTO" tolerates typos (best_fields/most_fields only — rejected outright by the
  live index for cross_fields, and meaningless for phrase types).
- tie_breaker (best_fields only, 0–1): how much non-winning fields still contribute.
- minimum_should_match: e.g. "2<75%" to require more term overlap on longer queries.
- phrase_boost: optionally bump exact-phrase matches in some fields without requiring them.

Use the profile (what the data is about), the golden summary, the leaderboard (what already
works), and the per-case diagnostics to reason about WHY cases fail, then propose fixes. The \
diagnostics are shown worst-first under the CURRENT best config's field weighting: for each \
case, which fields the ideal docs match or miss, and — in \
`distractors_outranking_best_ideal` — the non-relevant docs still beating the best ideal doc \
and the field each one wins on. A distractor winning on a field is a direct signal to \
down-weight that field or lean on one where the ideal docs score better. Vary the structures you \
return — different query types, field sets, and boost shapes — don't return near-duplicates. \
A numeric optimizer will fine-tune the exact boost numbers afterward, so focus on the \
structure and sensible starting boosts. Call the propose_candidates tool with your batch."""


def propose(profile, golden_summary, allowed_fields, current_boosts, leaderboard,
            diagnostics, n=6, model=DEFAULT_MODEL):
    """Return a list of proposed candidates in AGENT_CANDIDATE_SCHEMA shape (the same shape the
    Managed-agent path writes to candidates.json); tune.py runs both through the same
    to_candidate → normalize → optimize → confirm pipeline. Raises if the Anthropic SDK /
    credentials are unavailable — callers gate on --no-agent."""
    import anthropic  # imported here so --no-agent needs neither the package nor a key

    client = anthropic.Anthropic()
    stable = (
        SYSTEM_RULES
        + "\n\nINDEX PROFILE:\n" + json.dumps(profile_brief(profile), indent=2)
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
        # Thinking runs by default on claude-opus-5 and shares this budget with the
        # tool call, so leave real headroom — a truncated tool_use yields partial or
        # missing `input`. Kept under ~16k so the non-streaming call stays inside the
        # SDK's timeout guard. Don't "fix" the budget by disabling thinking: with
        # thinking off, opus-5 sometimes writes the tool call as plain text and emits
        # no tool_use block at all, which this function cannot distinguish from a
        # genuinely empty batch.
        max_tokens=16000,
        system=[{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "propose_candidates"},
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"{model} hit max_tokens before finishing the candidate batch — the proposal is "
            f"truncated, not empty. Lower --candidates-per-round or raise max_tokens in "
            f"propose.py (and switch to client.messages.stream if you go much above 16k)."
        )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_candidates":
            return list(block.input.get("candidates", []))
    return []
